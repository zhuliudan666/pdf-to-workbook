"""素材分类：先认「这是什么」，再决定用哪套预案。

## 为什么要有这一步

前面每个脚本都在回答「怎么做」：probe 说能不能做，segment 说切在哪，qa 说切得对不对。
但真正决定效率的是**动手之前那三分钟**：这本是真题卷还是讲义？扫描件还是文字层？
每章题号是不是从 1 重排？这些问题想错一个，后面整条流水线都白跑。

2026-11 之前，这些判断每次都在我脑子里从零做一遍：跑 probe、读报告、回忆「上次那种书
踩过什么坑」、凭印象填参数。同一类书第二次来，经验没有留下来。

`classify.py` 把这件事变成查表：
  指纹（一次扫描，全是可测的数） -> 打分匹配 `profiles/*.yaml` -> 一张处方

处方里最值钱的不是参数（config.yaml 已有默认值），是 **pitfalls**：
「这类书的跨页续文有 25 处」「末页会整页漏掉」「正文编号会被当成题号」——
这些只有踩过才知道，而且忘了就再踩一次。

## 指纹是什么

全部来自一次 `get_text` 扫描，不渲染、不 OCR，106 页约 1.5 秒：

| 信号 | 为什么能分类型 |
| --- | --- |
| `text_layer` | 扫描件 / OCR 层 / 文字层，决定走 OCR 还是正则 |
| `hits_per_page` | 每页几道题号：真题卷 5–10，讲义 0–1 |
| `head_hits` | 「命题点」「第X章」「习题」「一、选择题」各出现几次 |
| `num_chaptered_ratio` | 题号带不带章号（18.3 / 3.）。带章号的多是题库 |
| `answer_density` | 「解/证明/分析」行占比：解析册高，习题册低 |
| `images_per_page` | 整页图 = 扫描件；零星图 = 公式插图 |
| `two_col_pages` | 双栏页（复用 probe 的判据，不另写一份） |

不打分调参、不做模型：规则命中就是命中，错了一眼能看出是哪条判错了。
这种工具宁可笨一点，也不能给出一个查不出原因的置信度。

## 用法

    python classify.py 素材.pdf                  # 看处方
    python classify.py 素材.pdf --json out.json  # 给 intake / 流水线读
    python classify.py --list                    # 有哪些预案
    python classify.py 素材.pdf --why            # 连每个类型差在哪条都说出来
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import common
import probe

HERE = Path(__file__).resolve().parent.parent
PROFILES = HERE / "profiles"

# 标题类信号。这些不是「题目」，是**书的骨架**——类型基本靠它们分。
# 每条都对应一个真实见过的版式：命题点（某数三习题册）、第X章（讲义）、一、选择题（真题卷）、
# 例（教材正文）、解（解析册）。加新键时想清楚：它能不能把两类书分开？
HEAD_PATTERNS = {
    "命题点": r"命题点",
    "章": r"^\s*第\s*[一二三四五六七八九十百零〇\d]+\s*[章讲节部篇]\b",
    "节": r"^\s*\d{1,2}(?:\.\d{1,2}){1,2}\s+\S",
    "习题": r"^\s*(?:习题|练习|课后习题|练习题|Exercises|Problems)\s*[\d.．]*\s*$",
    "题型": r"^\s*[一二三四五六七八九十]\s*[、.．]\s*(?:选择题|填空题|解答题|证明题|计算题)",
    "例题": r"^\s*例\s*\d",
    "解答": r"^\s*(?:解|证明|分析|答案)\s*[:：]?",
}

# 只看这几行就知道书的骨架长什么样，给人核对的抓手
TITLE_SAMPLE = 12


def _ink_rows(page) -> int:
    """这一页有多少「行墨段」。

    不是像素行数——`_row_ink` 返回的是每一像素行有几个墨点，150dpi 下一页 1750 行，
    直接数非零会得到 800 多，跟版面没关系。要看的是「有几条横着的墨迹带」，
    所以数 0→有墨 的跳变。讲义正文页 40–60 条，题库题页 15–25 条，这个差别才分得开书。
    """
    ink, _ = common._row_ink(page)
    on = ink > 0
    if not len(on):
        return 0
    return int(on[0]) + int((~on[:-1] & on[1:]).sum())


def _sample_pages(n: int, k: int = 3) -> list[int]:
    """取 15% / 50% / 85% 三页当样本。避开首页（封面/目录）和末页（答案/空白）。"""
    if n <= 0:
        return []
    return sorted({min(n, max(1, int(round(n * f)))) for f in (0.15, 0.5, 0.85)})


def _ink_sample(src: str, pages: list[int]) -> list[int]:
    doc = pymupdf.open(src)
    rows = [_ink_rows(doc[p - 1]) for p in pages if p <= len(doc)]
    doc.close()
    return rows


def _scan_sample(src: str, cfg: dict, pages: list[int], out: Path | None = None) -> dict:
    """扫描件抽几页做左边条带 OCR。

    为什么必须有这一步：整页扫描件的文字指纹**全是 0**——没有文本层，题号、标题、
    行数一个都读不出来，分类器对它只能输出 unknown。而实际送来的书，扫描件占多数。

    为什么只 OCR 条带、只抽 3 页：全页 OCR 6.3 秒/页，抽 3 页也要 19 秒；左边条带
    （默认 175pt 宽）只占版面 1/4，3 页一两秒。条带恰好压在题号那一栏上，
    而「每页几道题号」正是分类型最有用的一个数。

    代价说实话：居中或靠右排的章节标题不在条带里，所以扫描件的骨架词会偏少。
    因此扫描件的判据不依赖「章」这类标题，只看题号密度 + 墨行密度。
    """
    doc = pymupdf.open(src)
    n = len(doc)
    doc.close()
    import tempfile

    o = out or (Path(tempfile.gettempdir()) / "pdf2wb_classify")
    idx = pages or _sample_pages(n)
    texts: list[str] = []
    per_page: list[int] = []
    try:
        import segment_ocr

        for p in idx:
            ls = segment_ocr.strip_lines(src, p, cfg, o)
            # OCR 行是 {"text","box":[x0,y0,x1,y1],"score"}，common.find_marks 要的是
            # 摊平的 x0/y0/x1/y1。这里转一次，别为了省事在 common 里加分支。
            norm = [{"text": ln.get("text", ""), "x0": ln["box"][0], "y0": ln["box"][1],
                     "x1": ln["box"][2], "y1": ln["box"][3]}
                    for ln in ls if ln.get("box")]
            per_page.append(max((len(common.find_marks(
                norm, {**cfg, "question_patterns": [pat]})) for pat in cfg["question_patterns"]),
                default=0))
            texts += [ln["text"].strip() for ln in ls if ln.get("text", "").strip()]
    except Exception as e:      # OCR 引擎缺依赖时不能把分类整个搞挂
        return {"pages": idx, "error": f"{type(e).__name__}: {e}", "marks": 0,
                "marks_per_page": 0.0, "per_page": [], "text": []}

    return {
        "pages": idx,
        "per_page": per_page,
        "marks": sum(per_page),
        "marks_per_page": round(sum(per_page) / max(len(idx), 1), 2),
        "text": texts[:40],
    }


def fingerprint(src: str, cfg: dict | None = None, ocr_sample: int = 3) -> dict:
    """一次扫描抽出所有判断用的数。不做取舍——取舍在 profile 里。"""
    p = Path(src)
    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"):
        from PIL import Image

        with Image.open(p) as im:
            w, h = im.size
        # 照片没有 dpi 元数据，按 A4 竖版反推（宽 210mm = 8.27in），和 intake 一个口径
        dpi = int(round(w / 8.27))
        return {
            "input": str(p.resolve()), "kind": "image", "pages": 1,
            "text_layer": "none", "chars_per_page": 0, "hits": 0, "hits_per_page": 0.0,
            "images_per_page": 1.0, "two_col_pages": [], "num_chaptered_ratio": 0.0,
            "answer_density": 0.0, "ink_rows_per_page": 0.0, "pages_with_marks_ratio": 1.0, "scan": None,
            "render_dpi": dpi, "pixels": [w, h],
            "head_hits": {k: 0 for k in HEAD_PATTERNS}, "title_lines": [],
        }
    cfg = cfg or common.load_config(str(HERE / "config.yaml"))
    doc = pymupdf.open(src)
    n = len(doc)
    chars = 0
    imgs = 0
    lines_total = 0
    two_col: list[int] = []
    heads = {k: 0 for k in HEAD_PATTERNS}
    pats = {k: re.compile(v) for k, v in HEAD_PATTERNS.items()}
    title_lines: list[tuple[int, str]] = []
    plain = chaptered = 0
    marked_pages = 0

    pat_hits = {p: 0 for p in cfg["question_patterns"]}
    for pno, page in enumerate(doc):
        chars += len("".join(page.get_text("text").split()))
        imgs += len(page.get_images(full=True))
        flag, _ = probe.is_two_column(page)
        if flag:
            two_col.append(pno + 1)
        lines = common.page_lines(page)
        lines_total += len(lines)
        # 题号命中按「每条正则各算一次」累计，取最大的一条当本书的题号数。
        # 只跑一次页面扫描：find_marks 是纯字符串匹配，用已经取到的行，不重新 open 文档。
        clines = common.content_lines(page, cfg)
        page_hits = 0
        for pat in cfg["question_patterns"]:
            n_hit = len(common.find_marks(clines, {**cfg, "question_patterns": [pat]}))
            pat_hits[pat] += n_hit
            page_hits = max(page_hits, n_hit)
        if page_hits:
            marked_pages += 1
        for ln in lines:
            t = ln["text"].strip()
            hit = False
            for k, rx in pats.items():
                if rx.match(t):
                    heads[k] += 1
                    hit = True
                    if k in ("章", "节", "习题", "题型", "命题点") and len(title_lines) < TITLE_SAMPLE:
                        title_lines.append((pno + 1, t[:40]))
            # 题号形态：18.3 这种带章号的，和「3.」这种纯序号，代表两种编号体系
            m = re.match(r"^\s*(\d+)\s*[.．、]\s*(\d+)", t)
            if m:
                chaptered += 1
            elif re.match(r"^\s*\d+\s*[.．、]\s*(?=\S)", t):
                plain += 1

    sig = probe.text_signals(doc)
    chars_per_page = chars / max(n, 1)
    layer = probe.classify_text_layer(chars_per_page, **sig)
    doc.close()

    hits = max(pat_hits.values()) if pat_hits else 0
    pages = _sample_pages(n)
    ink_rows = _ink_sample(src, pages)
    scan = None
    if layer != "ok":
        # 文字层不可信（没有，或是扫描件后面加的 OCR 层）→ 抽样 OCR 补指纹
        scan = _scan_sample(src, cfg, pages) if ocr_sample else None
        if scan and not scan.get("error"):
            hits = int(round(scan["marks_per_page"] * n))
            # 扫描件的「有题页比例」只能按抽样页估：3 页里有几页认出了题号
            if scan.get("per_page"):
                marked_pages = round(sum(1 for k in scan["per_page"] if k) / len(scan["per_page"]) * n)
            # 骨架词只能在条带里认；认到的算数，没认到不代表书里没有
            for t in scan["text"]:
                for k, rx in pats.items():
                    if rx.match(t):
                        heads[k] += 1
    total_num = plain + chaptered
    rows_avg = sum(ink_rows) / len(ink_rows) if ink_rows else 0.0
    return {
        "input": str(Path(src).resolve()),
        "kind": "pdf",
        "pages": n,
        "text_layer": layer,
        "chars_per_page": round(chars_per_page),
        "hits": hits,
        "hits_per_page": round(hits / max(n, 1), 2),
        "images_per_page": round(imgs / max(n, 1), 2),
        "two_col_pages": two_col,
        "num_chaptered_ratio": round(chaptered / total_num, 2) if total_num else 0.0,
        "answer_density": round(heads["解答"] / max(lines_total, 1), 3),
        "ink_rows_per_page": round(rows_avg, 1),
        "pages_with_marks_ratio": round(marked_pages / max(n, 1), 2),
        "scan": scan,
        "head_hits": heads,
        "title_lines": title_lines,
    }


def load_profiles() -> list[dict]:
    import yaml

    out = []
    for p in sorted(PROFILES.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        d["_file"] = p.name
        out.append(d)
    return out


def _in_range(v, rng) -> bool:
    lo, hi = (rng + [rng[0]])[:2] if isinstance(rng, list) else (rng, rng)
    return lo <= v <= hi


def check(fp: dict, when: dict) -> tuple[list[str], list[str]]:
    """返回 (命中的条件, 没命中的条件)。条件写得越具体，越能分开相近的类型。"""
    ok, bad = [], []
    for key, want in (when or {}).items():
        if key == "kind":
            got = fp["kind"]
            (ok if got in (want if isinstance(want, list) else [want]) else bad).append(
                f"输入 {got}")
        elif key == "text_layer":
            got = fp["text_layer"]
            (ok if got in (want if isinstance(want, list) else [want]) else bad).append(
                f"文字层 {got}")
        elif key == "two_col":
            got = bool(fp["two_col_pages"])
            (ok if got == bool(want) else bad).append(
                f"双栏 {'有' if got else '无'}{'' if got == bool(want) else f'（要{("有" if want else "无")}）'}")
        elif key == "head_hits":
            for hk, rng in want.items():
                got = fp["head_hits"].get(hk, 0)
                (ok if _in_range(got, rng) else bad).append(
                    f"「{hk}」{got} 处" + ("" if _in_range(got, rng) else f"（要 {rng}）"))
        elif key in fp and isinstance(want, list):
            got = fp[key]
            hitok = _in_range(got, want)
            (ok if hitok else bad).append(f"{_LABEL.get(key, key)} {got}" + ("" if hitok else f"（要 {want}）"))
        else:
            bad.append(f"未知条件 {key}")
    return ok, bad


_LABEL = {
    "pages": "页数",
    "hits_per_page": "每页题号",
    "images_per_page": "每页图",
    "chars_per_page": "每页字数",
    "num_chaptered_ratio": "带章号题号占比",
    "answer_density": "解答行占比",
    "pages_with_marks_ratio": "有题页比例",
}


def classify(fp: dict, profiles: list[dict] | None = None) -> dict:
    """打分：全中才算候选；全中的里面取条件最多的（更特异）。"""
    profiles = profiles if profiles is not None else load_profiles()
    cands = []
    for p in profiles:
        ok, bad = check(fp, p.get("when") or {})
        cands.append({"profile": p, "ok": ok, "bad": bad,
                      "coverage": len(ok) / max(len(ok) + len(bad), 1)})
    full = [c for c in cands if not c["bad"] and c["ok"]]
    full.sort(key=lambda c: -len(c["ok"]))
    best = full[0] if full else None
    near = max(cands, key=lambda c: c["coverage"]) if cands else None
    return {"pick": best, "cands": cands, "near": near}


# ---------------------------------------------------------------- 处方

def prescribe(fp: dict, pick: dict | None, near: dict | None) -> dict:
    if pick is None:
        return {
            "type": "unknown",
            "label": "没认出来",
            "confidence": round(near["coverage"], 2) if near else 0.0,
            "because": near["ok"] if near else [],
            "missing": near["bad"] if near else [],
            "route": "先人工看一眼：probe.py + 前 3 页",
            "pitfalls": ["没有匹配的预案，说明这是没处理过的版式。"
                         "跑一次 probe，把结论补成 profiles/ 里的新预案，下次就认得"],
            "expect": {},
            "verified": False,
        }
    p = pick["profile"]
    return {
        "type": p.get("id"),
        "label": p.get("label"),
        "confidence": 1.0,
        "because": pick["ok"],
        "missing": [],
        "route": p.get("route"),
        "room": p.get("room"),
        # 预案可以直接带一份配置片段（正则、切点停在哪），intake 认出来后写进
        # intake.yaml。没有这一项，「认出来了」和「用得上」之间还隔着手抄正则。
        "config": p.get("config") or {},
        "params": p.get("params") or {},
        "pitfalls": p.get("pitfalls") or [],
        "expect": p.get("expect") or {},
        "accept": p.get("accept"),
        "verified": bool(p.get("verified")),
        "sample": p.get("sample"),
        "file": p.get("_file"),
    }


def render(res: dict, fp: dict, why: bool = False) -> str:
    L = [f"# 素材分类：{Path(fp['input']).name}", ""]
    L.append(f"**{res['label']}**" + (f"（{res['type']}）" if res["type"] != "unknown" else ""))
    if res["type"] != "unknown":
        L.append("")
        L.append("判据：" + "；".join(res["because"]))
        if not res["verified"]:
            L.append("")
            L.append(f"> ⚠ 这条预案还没被真书验证过（{res.get('file')}）。"
                     "先用前 3 页试跑，确认切点再放开。")
    else:
        L.append("")
        L.append("最接近的一类差在这些条件上：" + "；".join(res["missing"]))
    L += ["", "## 指纹", "",
          f"- {fp['pages']} 页，文字层 {fp['text_layer']}，每页 {fp['chars_per_page']} 字",
          f"- 题号 {fp['hits']} 个（每页 {fp['hits_per_page']}），带章号的占 {fp['num_chaptered_ratio']:.0%}",
          f"- 每页图 {fp['images_per_page']}，双栏页 {len(fp['two_col_pages'])} 页"
          + (f"（{fp['two_col_pages'][:8]}）" if fp["two_col_pages"] else ""),
          "- 骨架：" + "、".join(f"{k} {v}" for k, v in fp["head_hits"].items() if v)]
    if fp["title_lines"]:
        L += ["", "标题抽样："]
        L += [f"  p{p} {t}" for p, t in fp["title_lines"][:6]]
    L += ["", "## 处方", "", f"- 路线：{res['route']}"]
    if res.get("expect"):
        L.append("- 预期：" + "；".join(f"{k} {v}" for k, v in res["expect"].items()))
    if res.get("accept"):
        L.append("- 验收重点：" + res["accept"])
    if res["pitfalls"]:
        L += ["", "已知坑（这类书踩过的，不是假设）：", ""]
        L += [f"- {x}" for x in res["pitfalls"]]
    if why:
        L += ["", "## 所有类型的匹配情况", ""]
        for c in res["_cands"]:
            mark = "✓" if c is res["_pick"] else " "
            L.append(f"{mark} {c['profile'].get('id'):<12} {c['coverage']:.0%}"
                     + ("  差：" + "；".join(c["bad"]) if c["bad"] else ""))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="认素材类型，给处理预案")
    ap.add_argument("src", nargs="?")
    ap.add_argument("--json", default="", help="把处方写成 json")
    ap.add_argument("--why", action="store_true", help="连每个类型差在哪条都说出来")
    ap.add_argument("--list", action="store_true", help="列出所有预案")
    ap.add_argument("--config", default="")
    a = ap.parse_args(argv)

    if a.list or not a.src:
        profs = load_profiles()
        print(f"预案目录：{PROFILES}\n")
        for p in profs:
            v = "已验" if p.get("verified") else "未验"
            print(f"  {p.get('id'):<12} [{v}] {p.get('label')}")
            print(f"               路线 {p.get('route')}   条件 {len(p.get('when') or {})} 条"
                  + (f"   样本 {p.get('sample')}" if p.get("sample") else ""))
        return 0

    fp = fingerprint(a.src, common.load_config(a.config) if a.config else None)
    out = classify(fp)
    res = prescribe(fp, out["pick"], out["near"])
    res["_cands"], res["_pick"] = out["cands"], out["pick"]
    print(render(res, fp, a.why))

    if a.json:
        payload = {k: v for k, v in res.items() if not k.startswith("_")}
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps({"prescription": payload, "fingerprint": fp},
                                           ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n-> {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
