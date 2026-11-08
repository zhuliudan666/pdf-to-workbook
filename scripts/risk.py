"""风险排序：校对单到底该先看哪几题。

原来的校对单是「无差别缩略图墙」——88 道题排成一墙，人扫到第 40 张就开始走神，
而真正出错的那几道散落在中间。本模块把三个信号源合成一个分数，让最可疑的题
出现在第一页。

三个来源，可靠性依次下降：
  1. structural  题号断了/重了/区间没了 —— 高危，一坏就是整册
  2. selfcheck   切点切穿了 —— 会误报（贴着墨切但无害），但方向对
  3. 图像特征    跨页、题高、公式密度 —— 只是「容易错的地方」，不是「已经错了」

分数不是概率，只用来排序。阈值和权重都是在这本书上量出来的，换书要重看
`counts` 的比例是否还合理（重点区落在 15%~30% 之间算正常）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import structural

# ---- 权重：改这里之前先想清楚它会让多少题挤进重点区 ----
W_STRUCT_HIGH = 100
W_STRUCT_MID = 45
W_CUT = 70          # 切点切穿 / 题高异常：几何判据直接判定，优先于人工扫
W_SPAN = 30         # 跨页题要人确认续文合并得对不对
W_DENSE = 26        # 公式密集：转写与裁切都容易出错
W_LONG = 8          # 长题：一页放不下，容易跨页

# 公式密集 = 平均行高在本册前 15%%，且绝对值不低于 30pt。
# 行高 20pt 左右是常规正文（10.5pt 字 + 行距），到 30pt 以上基本是因为
# 分式、积分号、求和上下限把行撑开了。
DENSE_PCT = 85
DENSE_MIN = 30.0

RED, AMBER, GRAY = 70, 25, 0

TAG_LEVEL = {RED: "red", AMBER: "amber", GRAY: "gray"}


def _bands(path: str) -> int:
    """数一张题图里有几个横向墨迹带（≈ 文本行数）。"""
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return 0
    try:
        with Image.open(path) as im:
            a = np.asarray(im.convert("L"))
    except Exception:
        return 0
    rows = (a < 160).sum(axis=1) >= 2
    runs, cur, gap = 0, 0, 0
    for v in rows:
        if v:
            cur += 1
            gap = 0
        elif cur:
            gap += 1
            if gap >= 3:          # 行内被笔画切开的 1-2 行空隙不算换行
                runs += 1
                cur = 0
    if cur:
        runs += 1
    return runs


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
    return s[k]


def features(pdf: str, questions: list[dict], out_dir: Path) -> dict[str, dict]:
    """每题的几何特征：高度、行数、平均行高。"""
    per: dict[str, dict] = {}
    lines_per_q = []
    for q in questions:
        h = 0.0
        for p in q.get("parts") or []:
            r = p.get("rect") or [0, 0, 0, 0]
            h += max(0.0, r[3] - r[1])
        n = _bands(q.get("image", "")) if q.get("image") else 0
        per[str(q["seq"])] = {
            "height": h,
            "lines": n,
            "line_h": (h / n) if n else 0.0,
        }
        if n:
            lines_per_q.append(h / n)
    return per


def collect(pdf: str, out_dir: str | Path, cfg: dict | None = None,
            questions: list[dict] | None = None,
            check: dict | None = None, struct: dict | None = None) -> dict:
    """把三路信号合成「排序后的校对清单」。

    check / struct 可传入已算好的结果，避免 build.py 里重复跑一遍。
    """
    out = Path(out_dir)
    if questions is None:
        questions = json.loads((out / "questions.json").read_text(encoding="utf-8"))

    if struct is None:
        struct = structural.check(questions)
    if check is None:
        import selfcheck

        check = selfcheck.run(pdf, str(out))

    feats = features(pdf, questions, out)

    # 公式密集的相对门槛：按本册的分布算，不硬编码某一个 pt 值
    p85 = _pct([f["line_h"] for f in feats.values() if f["line_h"]], DENSE_PCT)
    dense_thr = max(DENSE_MIN, p85)

    by_seq: dict[str, list[dict]] = {}
    for p in struct["problems"]:
        by_seq.setdefault(str(p["seq"]), []).append(p)
    for p in check["problems"]:
        by_seq.setdefault(str(p["seq"]), []).append(p)

    items = []
    for q in questions:
        seq = str(q["seq"])
        score = 0
        tags: list[tuple[int, str, str]] = []   # (级别, 短标签, 解释)

        for p in by_seq.get(seq, []):
            if p["kind"] in ("跳号", "题号重复", "题号乱序", "题区间为空", "题区间过窄"):
                score += W_STRUCT_HIGH
                tags.append((RED, p["kind"], p["detail"]))
            elif p["kind"] in ("顶部切穿内容", "底部切穿内容", "题高异常"):
                score += W_CUT
                tags.append((RED, "切点可疑", f"{p['kind']} {p['where']}"))
            else:
                score += W_STRUCT_MID
                tags.append((AMBER, p["kind"], p["detail"]))

        if q.get("span_pages", 1) > 1:
            score += W_SPAN
            tags.append((AMBER, "跨页", f"跨 {q['span_pages']} 页，确认续文合并是否完整"))

        f = feats[seq]
        if f["line_h"] and f["line_h"] >= dense_thr:
            score += W_DENSE
            tags.append((AMBER, "公式密集", f"平均行高 {f['line_h']:.0f}pt（本册前 15% 门槛 {dense_thr:.0f}pt）"))
        if f["height"] >= 260:
            score += W_LONG
            tags.append((GRAY, "长题", f"题高 {f['height']:.0f}pt"))

        tier = "red" if score >= RED else ("amber" if score >= AMBER else "gray")
        items.append(
            {
                "seq": q["seq"],
                "id": q["id"],
                "source_page": q.get("source_page", 0),
                "section": q.get("section", ""),
                "score": score,
                "tier": tier,
                "tags": [{"level": lv, "short": s, "why": w} for lv, s, w in tags],
                "image": q.get("image", ""),
                "height": round(f["height"], 1),
                "lines": f["lines"],
            }
        )

    items.sort(key=lambda x: (-x["score"], x["seq"]))
    counts = {"red": 0, "amber": 0, "gray": 0}
    for it in items:
        counts[it["tier"]] += 1

    return {
        "items": items,
        "counts": counts,
        "questions": len(questions),
        "structural": struct,
        "selfcheck": check,
        "dense_threshold": round(dense_thr, 1),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="校对单风险排序（供 review.py 使用，也可单独看）")
    ap.add_argument("pdf")
    ap.add_argument("out_dir")
    a = ap.parse_args()
    r = collect(a.pdf, a.out_dir)
    print(f"共 {r['questions']} 题：重点 {r['counts']['red']} / 关注 {r['counts']['amber']} / 抽检 {r['counts']['gray']}")
    for it in r["items"][:20]:
        names = "、".join(t["short"] for t in it["tags"])
        print(f"  score={it['score']:3d} {it['tier']:5s} {it['id']:6s} p{it['source_page']:<3d} {names}")
