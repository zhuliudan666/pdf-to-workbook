"""校对单：把「最可疑的题」排在前面，而不是把所有题排成一墙。

原来的做法是 88 道题无差别铺成缩略图墙。问题是人的注意力只在前二十张，
而真正切坏的那几道散在中间——墙越大，越没人真看。

现在分三段：
  摘要页  有多少题需要看、为什么，两列清单
  重点区  切点可疑 / 题号断了 / 跨页，2×2 大图，带原因标签
  抽检区  其余题目，3×4 小图，只用来确认没漏题

排序逻辑在 risk.py，本文件只负责把结论画出来。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pymupdf
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import common
import risk

COLORS = {
    "red": (0.80, 0.18, 0.18),
    "amber": (0.82, 0.52, 0.06),
    "gray": (0.76, 0.76, 0.76),
}
TIER_NAME = {"red": "重点", "amber": "关注", "gray": "抽检"}

THUMB_Q = 72          # JPEG 质量，缩略图够看即可，别把 300dpi 原图塞进校对单


def _text(page, rect, txt, size, color, font, align=0):
    """写一行字。insert_textbox 高度不够时返回负值且**什么都不画**（不是截断），
    所以这里失败就往下扩一点重试，否则会静默丢标题。"""
    if not font:
        return
    r = pymupdf.Rect(rect)
    for _ in range(3):
        try:
            left = page.insert_textbox(r, txt, fontname=font[0], fontfile=font[1],
                                       fontsize=size, color=color, align=align)
        except Exception:
            return
        if left >= 0:
            return
        r.y1 += (-left) + size * 0.4


def _chip(page, x, y, color, size=4.0):
    page.draw_rect(pymupdf.Rect(x, y, x + size, y + size), color=color, fill=color, width=0)


def _summary_page(doc, ranked, font, nav, pno=0):
    W, H = common.PAPER_SIZES["A4"]
    page = doc.new_page(pno=pno, width=W, height=H)
    M = 40

    _text(page, pymupdf.Rect(M, 34, W - M, 58), "校对单 · 风险评估", 12, (0.1, 0.1, 0.1), font)
    c = ranked["counts"]
    _text(
        page,
        pymupdf.Rect(M, 60, W - M, 76),
        f"共 {ranked['questions']} 题　　重点 {c['red']} · 关注 {c['amber']} · 抽检 {c['gray']}",
        8.5,
        (0.35, 0.35, 0.35),
        font,
    )

    # 结构校验结论：这一段是「整册是否可信」的底线，放在最上面
    st = ranked.get("structural") or {}
    secs = st.get("sections") or {}
    y = 88
    if secs:
        parts = [f"{k} {v['count']}题（{v['first']}→{v['last']}，p{v['page_from']}-p{v['page_to']}）"
                 for k, v in secs.items()]
        _text(page, pymupdf.Rect(M, y, W - M, y + 16), "结构：" + "　".join(parts), 7.5, (0.3, 0.3, 0.3), font)
        y += 17
    highs = [p for p in (st.get("problems") or []) if p["severity"] == "high"]
    if highs:
        _text(page, pymupdf.Rect(M, y, W - M, y + 16),
              f"结构校验有 {len(highs)} 处高危（题号跳号/重号），先处理再交付", 8, COLORS["red"], font)
    else:
        _text(page, pymupdf.Rect(M, y, W - M, y + 16),
              "结构校验：题号连续、无重号" if not st.get("problems") else "结构校验：无高危问题",
              8, (0.15, 0.5, 0.25), font)
    y += 28

    # 需要看的题，两列清单
    must = [it for it in ranked["items"] if it["tier"] in ("red", "amber")]
    _text(page, pymupdf.Rect(M, y, W - M, y + 16),
          f"需要看的 {len(must)} 题（按怀疑程度排序，从上往下看）", 9, (0.15, 0.15, 0.15), font)
    y += 18

    col_w = (W - M * 2 - 16) / 2
    per_col = 22
    if len(must) <= 6:
        columns = [must[:per_col]]
    else:
        k = min(per_col, (len(must) + 1) // 2)
        columns = [must[:k], must[k:k + per_col]]
    rows_max = 0
    for ci, chunk in enumerate(columns):
        cy = y
        for it in chunk:
            x = M + ci * (col_w + 16)
            _chip(page, x, cy + 2.2, COLORS[it["tier"]], 4.2)
            tag = "·".join(t["short"] for t in it["tags"])
            _text(page, pymupdf.Rect(x + 7, cy, x + col_w - 4, cy + 12),
                  f"#{it['seq']:>3}  {it['id']}   p{it['source_page']}   {tag}", 7, (0.22, 0.22, 0.22), font)
            cy += 11.4
        rows_max = max(rows_max, len(chunk))
    shown = sum(len(c) for c in columns)
    y += rows_max * 11.4 + 6
    if len(must) > shown:
        _text(page, pymupdf.Rect(M, y, W - M, y + 14),
              f"…另有 {len(must) - shown} 题，见重点区各页", 7, (0.5, 0.5, 0.5), font)
        y += 14

    # 章节分布：一眼看出风险集中在哪一章
    y += 16
    _text(page, pymupdf.Rect(M, y, W - M, y + 14), "风险分布", 9, (0.15, 0.15, 0.15), font)
    y += 16
    cols = [(M, "章节"), (M + 110, "题数"), (M + 165, "页码"), (M + 265, "重点"), (M + 315, "关注"), (M + 365, "抽检")]
    for x, name in cols:
        _text(page, pymupdf.Rect(x, y, x + 90, y + 12), name, 7, (0.45, 0.45, 0.45), font)
    y += 12

    buckets: dict[str, dict] = {}
    for it in ranked["items"]:
        key = it.get("section") or "正文"
        b = buckets.setdefault(key, {"n": 0, "red": 0, "amber": 0, "gray": 0, "p": set()})
        b["n"] += 1
        b[it["tier"]] += 1
        b["p"].add(it["source_page"])
    for key, b in buckets.items():
        pgs = sorted(b["p"])
        vals = [key, str(b["n"]), f"p{pgs[0]}-p{pgs[-1]}", str(b["red"] or "-"), str(b["amber"] or "-"), str(b["gray"])]
        colors = [(0.25, 0.25, 0.25)] * 3 + [
            COLORS["red"] if b["red"] else (0.65, 0.65, 0.65),
            COLORS["amber"] if b["amber"] else (0.65, 0.65, 0.65),
            (0.45, 0.45, 0.45),
        ]
        for (x, _), v, col in zip(cols, vals, colors):
            _text(page, pymupdf.Rect(x, y, x + 90, y + 12), v, 7, col, font)
        y += 12

    # 页面导航
    if nav.get("focus"):
        _text(page, pymupdf.Rect(M, y + 10, W - M, y + 24),
              f"重点区 p{nav['focus'][0]}–p{nav['focus'][1]}　　抽检区 p{nav['scan'][0]}–p{nav['scan'][1]}",
              7.5, (0.4, 0.4, 0.4), font)

    # 怎么看
    yb = H - 92
    page.draw_line(pymupdf.Point(M, yb - 8), pymupdf.Point(W - M, yb - 8), color=(0.85, 0.85, 0.85), width=0.5)
    tips = [
        "红框 = 必看：切点切穿内容，或题号跳号/重号。这类错不会自己暴露，只能人看。",
        "黄框 = 建议看：跨页合并、公式密集（平均行高明显偏大），出错概率高但自检没判死。",
        "灰框 = 抽检：整页扫一眼，确认没有题目缺失即可，不必逐题核。",
        "判据是几何与编号，不是语义。切点落在空白带里不等于内容正确，这张单子只能缩小范围。",
    ]
    for i, t in enumerate(tips):
        _text(page, pymupdf.Rect(M, yb + i * 13, W - M, yb + i * 13 + 12), t, 7, (0.42, 0.42, 0.42), font)
    return page


def _flow_pages(doc, items, cols, font, header, thumb_scale, show_tags):
    """按题目图的实际长宽比排，而不是把整页均分成固定格子。

    题目图普遍又宽又扁（2000×400 这种），固定格子会把大半格留白，一页只放 4 题
    还看不清。改成「每行 cols 张、行高取该行最高那张」，页面高度用满了才算数。
    """
    W, H = common.PAPER_SIZES["A4"]
    margin, gutter = 26, 14
    top, bottom = 42, H - 26
    cell_w = (W - margin * 2 - gutter * (cols - 1)) / cols
    hdr_h = 22 if show_tags else 13
    row_gap = 9
    avail = bottom - top

    prepared = []
    for it in items:
        im, iw, ih = None, 0, 0
        if it.get("image"):
            try:
                im = Image.open(it["image"]).convert("RGB")
                iw, ih = im.size
            except Exception:
                im = None
        if not im:
            prepared.append((it, None, cell_w, 24))
            continue
        w, h = cell_w, cell_w * ih / iw
        if h > avail - hdr_h - 8:                 # 单张比一页还高，按高度反算宽度
            h = avail - hdr_h - 8
            w = h * iw / ih
        prepared.append((it, im, w, h))

    pages: list = []
    page, y = None, 0

    def new_page():
        nonlocal page, y
        page = doc.new_page(width=W, height=H)
        pages.append(page)
        y = top

    new_page()
    for start in range(0, len(prepared), cols):
        row = prepared[start:start + cols]
        row_h = max(p[3] + hdr_h + 4 for p in row)
        if y + row_h > bottom + 1:
            new_page()
        for i, (it, im, w, h) in enumerate(row):
            x = margin + i * (cell_w + gutter)
            page.draw_rect(pymupdf.Rect(x, y, x + cell_w, y + row_h),
                           color=COLORS.get(it["tier"], COLORS["gray"]), width=1.1)
            _text(page, pymupdf.Rect(x + 4, y + 2, x + cell_w - 4, y + 12),
                  f"#{it['seq']}  {it['id']}   p{it['source_page']}", 7, (0.2, 0.2, 0.2), font)
            if show_tags and it["tags"]:
                tags = "·".join(t["short"] for t in it["tags"])
                _text(page, pymupdf.Rect(x + 4, y + 11, x + cell_w - 4, y + hdr_h),
                      tags, 6.5, COLORS.get(it["tier"], (0.3, 0.3, 0.3)), font)
            if im is None:
                _text(page, pymupdf.Rect(x + 4, y + hdr_h, x + cell_w - 4, y + hdr_h + 12),
                      "（无缩略图）", 7, (0.5, 0.5, 0.5), font)
                continue
            box = pymupdf.Rect(x + 4, y + hdr_h, x + 4 + w, y + hdr_h + h)
            buf = io.BytesIO()
            im.resize((max(1, int(w * thumb_scale)), max(1, int(h * thumb_scale))), Image.LANCZOS) \
              .save(buf, format="JPEG", quality=THUMB_Q, optimize=True)
            page.insert_image(box, stream=buf.getvalue())
        y += row_h + row_gap

    for i, p in enumerate(pages):
        _text(p, pymupdf.Rect(margin, 18, W - margin, 32),
              f"{header}　第 {i + 1} / {len(pages)} 页", 8, (0.3, 0.3, 0.3), font)

    return pages


def main(pdf: str, cfg_path: str | None = None, out_dir: str | None = None,
         check: dict | None = None, struct: dict | None = None) -> str:
    out = Path(out_dir or Path(pdf).parent / "workbook_out")
    ranked = risk.collect(pdf, out, questions=None, check=check, struct=struct)

    font_path = common.cjk_font()
    font = ("cjk", font_path) if font_path else None

    doc = pymupdf.open()

    focus = [it for it in ranked["items"] if it["tier"] in ("red", "amber")]
    rest = [it for it in ranked["items"] if it["tier"] == "gray"]

    # 先排图，再把摘要页插到最前面——这样摘要里写的页码范围是实际值，不是估算
    fp = _flow_pages(doc, focus, 1, font, "重点区 · 红黄框必看", 2.0, True) if focus else []
    sp = _flow_pages(doc, rest, 3, font, "抽检区 · 灰框扫一眼", 1.3, False) if rest else []
    nf, ns = len(fp), len(sp)
    nav = {
        "focus": (2, 1 + nf) if nf else None,
        "scan": (2 + nf, 1 + nf + ns) if ns else None,
    }
    pages = [_summary_page(doc, ranked, font, nav, pno=0)]

    total = doc.page_count
    for i, p in enumerate(doc):
        _text(p, pymupdf.Rect(0, p.rect.height - 26, p.rect.width, p.rect.height - 14),
              f"校对单 {i + 1} / {total}", 7, (0.55, 0.55, 0.55), font, align=1)

    target = out / "校对单.pdf"
    try:
        doc.subset_fonts()
    except Exception:
        pass
    doc.save(target, garbage=4, deflate=True, clean=True)
    doc.close()

    c = ranked["counts"]
    print(f"校对单 -> {target}　共 {total} 页（重点 {c['red']} / 关注 {c['amber']} / 抽检 {c['gray']}）")
    return str(target)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python review.py <pdf> [config.yaml] [输出目录]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
