"""制作说明：把「这一本做了哪些判断、哪些吃了默认、哪些没验」写成随成品走的文件。

纯脚本交付的是一堆文件；这本书为什么长这样，之前只活在聊天里，换台机器就没了。
这份说明全部读回产物生成，不靠模型记忆：
  配置与技能默认值的差异 -> 判断
  intake 按档位算出来的值 -> 默认
  accept.json 的 warnings / qa.csv 有标 / 文字层缺口 -> 没验
  split_volumes.py 的 split.json -> 输入侧拆页的依据
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

HERE = Path(__file__).resolve().parent.parent

# intake 按留白档位算出来的键，不是「为这本书做的判断」
ROOM_KEYS = {"page_quota", "page_quota_mixed", "answer_gap_floor_by_section",
             "answer_gap_floor_mm", "answer_gap_cap_by_section", "answer_gap_cap_mm"}
JUDGE_KEYS = {"chapters", "question_patterns", "ignore_patterns", "cut_stop_patterns",
              "pages", "header", "content_box", "ocr", "text_layer_source", "align"}


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 产物缺了不该让说明书崩掉
        return default


def _diff_judgements(cfg: dict, base: dict) -> list[str]:
    out = []
    chaps = cfg.get("chapters") or []
    if chaps:
        out.append("分册：%d 册 —— %s" % (
            len(chaps), "、".join("%s（P%s）" % (c.get("title"), c.get("pages")) for c in chaps)))
    pats = cfg.get("question_patterns") or []
    defaults = base.get("question_patterns") or []
    extra = [p for p in pats if p not in defaults]
    if extra:
        out.append("题号正则相对默认补了 %d 条：%s" % (
            len(extra), "、".join("`%s`" % p for p in extra)))
    h = cfg.get("header") or {}
    if (h.get("title") or "").strip():
        out.append("页眉书名：%s" % h["title"].strip())
    elif (h.get("book") or "").strip():
        out.append("页眉书名：%s" % h["book"].strip())
    if cfg.get("pages"):
        out.append("只做指定页段：%s" % cfg["pages"])
    others = [k for k in JUDGE_KEYS - {"chapters", "question_patterns", "header", "pages"}
              if k in cfg and cfg[k] != base.get(k)]
    if others:
        out.append("另有这些项为这本书改过：%s" % "、".join(sorted(others)))
    return out


def _defaults_used(cfg: dict, base: dict) -> list[str]:
    q = cfg.get("page_quota") or {}
    floors = cfg.get("answer_gap_floor_by_section") or {}
    paper = cfg.get("paper") or {}
    rows = [
        "纸张：%s，边距 %s/%s/%s/%s pt" % (paper.get("size", "A4"), paper.get("margin_top"),
                                      paper.get("margin_bottom"), paper.get("margin_left"),
                                      paper.get("margin_right")),
        "每页题数：选填 %s 道、大题 %s 道（混排 %s 道）" % (
            q.get("选择题", "—"), q.get("解答题", "—"), cfg.get("page_quota_mixed", "—")),
        "答题区目标：选择 %s / 填空 %s / 解答 %s mm" % (
            floors.get("选择题", "—"), floors.get("填空题", "—"), floors.get("解答题", "—")),
        "留白样式：%s；文字层：%s（来源 %s）" % (
            cfg.get("gap_style", "—"), cfg.get("text_layer", True),
            cfg.get("text_layer_source", "auto")),
        "每题标原书页码：%s；目录：%s" % (
            (cfg.get("header") or {}).get("show_source_page", True), cfg.get("toc", True)),
    ]
    if not (cfg.keys() & ROOM_KEYS):
        rows.append("（档位项全部按 intake 的默认值生成，没手工改过）")
    return rows


def _gaps(out: Path, cfg: dict, acc: dict, qs: list[dict]) -> list[str]:
    rows = []
    for w in acc.get("warnings") or []:
        rows.append("验收闸：%s" % w)
    if cfg.get("text_layer", True):
        miss = [q for q in qs if not q.get("texts")]
        if miss:
            rows.append("文字层：%d 题搜不到题干（%s）" % (
                len(miss), "、".join("%s p%s 第%s题" % (
                    q.get("chapter") or "", q.get("source_page"), q.get("id"))
                    for q in miss[:8])))
    qa = out / "qa.csv"
    if qa.exists():
        try:
            lines = qa.read_text(encoding="utf-8-sig").splitlines()[1:]
            marked = [l for l in lines if l.strip() and l.rsplit(",", 1)[-1].strip() not in ("[]", "")]
            if marked:
                rows.append("切点质检：%d 片有标（切穿/窄缝/右贴边），见 qa.csv 与联系表" % len(marked))
        except Exception:  # noqa: BLE001
            pass
    st = _load(out / "structural.json", {})
    highs = [p for p in (st.get("problems") or []) if p.get("severity") == "high"]
    if highs:
        rows.append("结构校验：%d 处高危（%s）" % (
            len(highs), "、".join(p.get("kind", "") for p in highs[:5])))
    sc = _load(out / "selfcheck.json", {})
    if sc.get("problems"):
        rows.append("切点自检：%d 处问题（%s）" % (
            len(sc["problems"]),
            "、".join(sorted({str(p.get("kind", "")) for p in sc["problems"]}))))
    if not rows:
        rows.append("无：所有验收闸都覆盖到了，文字层、切点、结构校验均无遗留")
    return rows


def main(pdf: str, cfg_path: str | None, out_dir: str) -> Path:
    out = Path(out_dir)
    cfg = common.load_config(cfg_path)
    # 基准要带上 common.DEFAULTS，不能只比 config.yaml：
    # content_box / ocr 这些是 DEFAULTS 里的值，intake 原样抄进配置，
    # 只比 config.yaml 会把它们误报成「为这本书改过」。
    base = common.load_config(str(HERE / "config.yaml"))
    acc = _load(out / "accept.json", {})
    qs = _load(out / "questions.json", [])
    split = _load(out / "split.json", {})

    target = Path(pdf)
    size = target.stat().st_size / 1048576 if target.exists() else 0
    ok = acc.get("ok")
    verdict = "通过" if ok else ("有硬伤" if ok is False else "未跑验收")

    md = ["# 制作说明", "",
          "成品：`%s`" % target.name,
          "题数 / 页数：%d 题 / %d 页；文件 %.2f MB" % (len(qs), acc.get("pages", 0), size),
          "验收：%s" % verdict, ""]

    md += ["## 为这本书做的判断", ""]
    judge = _diff_judgements(cfg, base)
    if split.get("splits"):
        md.append("输入侧拆页：原 %d 页 → %d 页，丢掉目录页 %s" % (
            split.get("source_pages", "—"), split.get("out_pages", "—"),
            "、".join("p%d" % p for p in split.get("dropped_pages") or []) or "无"))
        for s in split["splits"]:
            md.append("  - 原 p%d「%s」标题 y=%.1f 处拦腰拆开：上半到 %.1f，下半上移 %.1f pt" % (
                s["source_page"], s["volume"], s["title_y"][0], s["upper"][1], s["shift"]))
        md.append("  依据：`%s`" % split.get("out", ""))
    md += ["- " + s for s in judge] if judge else ["- 无：全部按默认配置跑完"]
    md.append("")

    md += ["## 吃了默认（没为这本改过）", ""]
    md += ["- " + s for s in _defaults_used(cfg, base)]
    md.append("")

    md += ["## 没验到 / 要人再看一眼", ""]
    md += ["- " + s for s in _gaps(out, cfg, acc, qs)]
    md.append("")

    md += ["## 复跑", "",
          "```bash",
          'python scripts/build.py "%s" "%s" "%s"' % (
              (split.get("out") or str(target)), cfg_path or "config.yaml", out),
          "```", ""]

    f = out / "制作说明.md"
    f.write_text("\n".join(md), encoding="utf-8")
    print("制作说明 -> %s" % f)
    return f


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("用法: python report.py <做题本.pdf> [config.yaml] <输出目录>")
    if len(sys.argv) >= 4:
        main(sys.argv[1], sys.argv[2], sys.argv[3])
    else:
        main(sys.argv[1], None, sys.argv[2])
