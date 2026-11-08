"""自检：在打开成品之前，先把「切坏了」的题揪出来。

判据是「切点是否落在原书的空白带里」。切在空白带中心，本题就完整；切在墨迹
上，要么顶部被削一条，要么把上下题的内容带进来。

注意不能用成品图的边缘像素来判：crop.py 里的 vtrim 会主动把题图上下方的空白
裁掉，最后一笔墨迹本来就贴着边，那样全是误报。

两种输出：
  默认      人看的文本，一行一条问题
  --json    给 diagnose.py 吃的结构化结果（带 axis，便于判断是上边界还是下边界出错）

退出码：0 全部通过，1 有问题。build.py 依赖这个语义。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import common
import segment_ocr

EDGE_PX = 1     # 切点上下各看一个像素行：切点所在的那几行本身有墨，才算切穿了字
MIN_INK = 20    # 一行至少这么多个深色像素才算有内容，否则是扫描噪点
MIN_Q_HEIGHT = 22.0   # 低于这个高度，基本可以断定吸到了题目内部的空隙


def _problem(q: dict, page: int, kind: str, axis: str, value: float, where: str) -> dict:
    return {
        "seq": q["seq"],
        "id": q["id"],
        "source_page": q["source_page"],
        "page": page,
        "kind": kind,
        "axis": axis,        # top / bottom / height
        "value": round(value, 1),
        "where": where,
    }


def run(pdf: str, out_dir: str, cfg_path: str | None = None) -> dict:
    cfg = common.load_config(cfg_path)
    out = Path(out_dir)
    questions = json.loads((out / "questions.json").read_text(encoding="utf-8"))
    doc = pymupdf.open(pdf)

    page_h = doc[0].rect.height
    o = cfg["ocr"]
    content_top = o.get("content_top", 40)
    content_bottom = page_h - o.get("content_bottom_margin", 45)

    ink_cache: dict[int, object] = {}

    def ink_of(page_index: int):
        if page_index not in ink_cache:
            ink_cache[page_index] = segment_ocr.row_ink(doc[page_index])
        return ink_cache[page_index]

    def cut_through(page_index: int, y: float) -> bool:
        """切点那几行本身有墨 = 一刀切在文字/公式中间。

        不能按「切点两侧有没有墨」判：密排试卷的题间距只有 4–5pt，切在正中时
        两侧各差 2.4pt，按「两侧有墨」会把好好的切点全报成切穿。2024 数三的
        第 13、15 题就是这么误报的——它们切在 4.8pt 的缝正中，那一行纯白，
        却被两侧的字连累，白占了校对单的重点区，真正该看的题被挤到后面。
        """
        dark = ink_of(page_index)
        idx = int(round(y / 72.0 * segment_ocr.INK_DPI))
        lo, hi = max(0, idx - EDGE_PX), min(len(dark), idx + EDGE_PX + 1)
        if lo >= hi:
            return False
        hits = sum(1 for i in range(lo, hi) if dark[i] > MIN_INK)
        return hits >= 2

    line_cache: dict[int, list[tuple[float, float]]] = {}

    def has_full_line(page_index: int, top: float, bottom: float) -> bool:
        """区间里有没有**完整**的一行文字（文字层）。

        OCR 书没有文字层，这里恒为 False，退回原来的纯高度判据。
        """
        if page_index not in line_cache:
            line_cache[page_index] = [
                (l["bbox"][1], l["bbox"][3])
                for b in doc[page_index].get_text("dict")["blocks"]
                for l in b.get("lines", [])
            ]
        return any(top - 1.0 <= y0 and y1 <= bottom + 1.0 for y0, y1 in line_cache[page_index])

    problems = []
    for q in questions:
        for part in q["parts"]:
            pi = part["page"]
            top, bottom = part["rect"][1], part["rect"][3]
            if top > content_top + 1 and cut_through(pi, top):
                problems.append(_problem(q, pi, "顶部切穿内容", "top", top, f"p{pi + 1} y={top:.1f}"))
            if bottom < content_bottom - 1 and cut_through(pi, bottom):
                problems.append(_problem(q, pi, "底部切穿内容", "bottom", bottom, f"p{pi + 1} y={bottom:.1f}"))
            if bottom - top < MIN_Q_HEIGHT and len(q["parts"]) == 1 and not has_full_line(pi, top, bottom):
                # 跨页题的第一段本来就只占页底一小条，不能当异常。
                # 反过来，"矮"也不等于切坏了：单行填空题本来就矮——2024 数三第 13 题
                # 整题只有一行正文，16pt，被 22pt 的硬门槛报成疑似切穿，白占重点区。
                # 真正的判据是这一刀有没有把整行切掉：区间里装不下一整行字才算数。
                problems.append(_problem(q, pi, "题高异常", "height", bottom - top, f"{bottom - top:.1f}pt"))

    pages = doc.page_count
    doc.close()

    return {
        "pdf": str(Path(pdf).resolve()),
        "out_dir": str(out),
        "pages": pages,
        "questions": len(questions),
        "problems": problems,
        "by_kind": dict(Counter(p["kind"] for p in problems)),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="切点自检")
    ap.add_argument("pdf")
    ap.add_argument("out_dir")
    ap.add_argument("config", nargs="?", default=None)
    ap.add_argument("--json", dest="json_path", default=None, help="结构化结果写到这个路径")
    a = ap.parse_args(argv)

    r = run(a.pdf, a.out_dir, a.config)
    if a.json_path:
        Path(a.json_path).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")

    if not r["problems"]:
        print(f"自检通过：{r['questions']} 题，所有切点都落在原书空白带内")
        return 0

    print(f"自检发现 {len(r['problems'])} 处问题（{r['questions']} 题）：")
    for p in r["problems"]:
        print(f"  seq={p['seq']:3d} {p['id']:6s} 原书 p{p['source_page']:2d}  {p['kind']}  {p['where']}")
    print("\n下一步：python diagnose.py --check %s --questions %s"
          % (a.json_path or "<selfcheck.json>", str(Path(a.out_dir) / "questions.json")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
