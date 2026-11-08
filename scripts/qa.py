"""切点质检表：把「要不要看图」从 22 次变成 1 次。

review.py 是给用户看的（重点/关注/抽检，按风险排序）；本文件是给**改切点逻辑的人**
看的量化表。为什么需要它：2024 数三那一轮，22 道题我看了 35 张图才把 6 处切错找出来，
而错法其实只有三类，每一类都能用一个数判出来：

  窄缝  切点落在 <3.5pt 的缝里。原书的题间距一般 5–10pt，3pt 以下的缝基本都在
        公式内部（∫ 的上限与上一行的间距、∑ 与 ∞ 之间）。这一条能抓住最多的错：
        第 3 题那个 ∞、第 8 题那个分母 2、第 21 题那 0.4pt 的缝，都是窄缝。
  切穿  切点落在墨块内部——多切了字，或者把上一题的尾笔带进来。
  右贴边 最后一个墨点紧贴裁剪右边：右边界取窄了，字被裁掉（第 22 题就是这么丢的 "X_n"）。

三件产物（都在 out/ 下）：
  qa.csv        每题一行，改完代码 diff 一下就知道哪几题的切点动了、动了几 pt
  联系表.png     所有题缩略图拼一页，红框=有标的，一眼扫完
  qa.md         表格 + 结论（没标就一句话打发，不用点开任何图）

一开始还试过"顶部/底部残留高度 <5pt"和"题号上方有整行文字"两条，噪声太大：
∑ 上方的 ∞ 是本题自己的内容（会误报），矩阵的每一行都算"题号上方的行"（10/23 全红）。
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import common

NARROW_PT = 3.5     # 切点所在空白带窄于这个值，基本可以断定切进了公式内部
EDGE_PT = 0.8       # 离裁剪边这么近还有墨，算贴边
PAGE_EDGE_PT = 45.0  # 离页边这么近的切点不判切穿（那是版心边界，本来就要切）


def gap_at(cut: float, gaps: list[tuple[float, float]]) -> tuple[float, float, float]:
    """切点所在的空白带：返回 (上余墨, 下余墨, 带宽)。切点不在缝里就返回 0。"""
    for a, b in gaps:
        if a - 0.6 <= cut <= b + 0.6:
            return cut - a, b - cut, b - a
    return 0.0, 0.0, 0.0


def part_metrics(rect, blocks, lines, gaps, page_h) -> dict:
    x0, top, x1, bot = rect
    m = {"top": top, "bot": bot, "h": bot - top}

    m["切穿顶"] = int(top > PAGE_EDGE_PT and any(a < top - 0.6 < b for a, b in blocks))
    m["切穿底"] = int(bot < page_h - PAGE_EDGE_PT and any(a < bot - 0.6 < b for a, b in blocks))

    ut, ub, uw = gap_at(top, gaps)
    dt, db, dw = gap_at(bot, gaps)
    m["顶缝"], m["顶余"] = round(uw, 2), round(min(ut, ub), 2)
    m["底缝"], m["底余"] = round(dw, 2), round(min(dt, db), 2)

    inside = [l for l in lines if top - 0.5 <= l["y0"] and l["y1"] <= bot + 0.5]
    right = max((l["x1"] for l in inside), default=0.0)
    m["右贴边"] = int(bool(right) and x1 - right < EDGE_PT)
    return m


def flags(m: dict) -> list[str]:
    out = []
    if m["切穿顶"]:
        out.append("切穿顶")
    if m["切穿底"]:
        out.append("切穿底")
    if 0 < m["顶缝"] < NARROW_PT:
        out.append("顶窄缝%.1f" % m["顶缝"])
    if 0 < m["底缝"] < NARROW_PT:
        out.append("底窄缝%.1f" % m["底缝"])
    if m["右贴边"]:
        out.append("右贴边")
    return out


def contact_sheet(out: Path, flagged: set[int], dst: Path, cols: int = 4, thumb_w: int = 470) -> int:
    from PIL import Image, ImageDraw

    files = sorted((out / "crops").glob("*.png"))
    if not files:
        return 0
    thumbs = []
    for f in files:
        im = Image.open(f).convert("RGB")
        h = max(1, int(im.height * thumb_w / im.width))
        thumbs.append((f.name, im.resize((thumb_w, h), Image.LANCZOS)))
    gap, pad = 10, 16
    rows = (len(thumbs) + cols - 1) // cols
    cell_h = max(t.height for _, t in thumbs)
    W = cols * thumb_w + (cols + 1) * gap
    H = rows * (cell_h + pad) + (rows + 1) * gap
    sheet = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(sheet)
    for i, (name, t) in enumerate(thumbs):
        r, c = divmod(i, cols)
        x = gap + c * (thumb_w + gap)
        y = gap + r * (cell_h + pad + gap)
        sheet.paste(t, (x, y))
        seq = int(name.split("_")[0])
        hot = seq in flagged
        d.rectangle([x - 1, y - 1, x + thumb_w, y + t.height],
                    outline=(200, 20, 20) if hot else (198, 198, 198), width=2 if hot else 1)
        d.text((x + 3, y + t.height + 2), name.split("_")[0],
               fill=(180, 20, 20) if hot else (110, 110, 110))
    sheet.save(dst)
    return len(thumbs)


def main(pdf: str, cfg_path: str | None = None, out_dir: str | None = None) -> dict:
    cfg = common.load_config(cfg_path)
    out = Path(out_dir or (Path(pdf).parent / "workbook_out"))
    questions = json.loads((out / "questions.json").read_text(encoding="utf-8"))
    doc = pymupdf.open(pdf)

    cache: dict[int, tuple] = {}
    rows = []
    for q in questions:
        for part in q["parts"]:
            pno = part["page"]
            if pno not in cache:
                page = doc[pno]
                blocks = common.ink_blocks(page)
                cache[pno] = (blocks, common.ink_gaps(page, blocks=blocks),
                              common.content_lines(page, cfg), page.rect.height)
            blocks, gaps, lines, ph = cache[pno]
            m = part_metrics(part["rect"], blocks, lines, gaps, ph)
            m.update({"seq": q["seq"], "id": q["id"], "page": pno + 1,
                      "kind": part["kind"], "flags": flags(m)})
            rows.append(m)
    doc.close()

    flagged = {r["seq"] for r in rows if r["flags"]}
    n_thumbs = contact_sheet(out, flagged, out / "联系表.png")

    cols = ["seq", "id", "page", "kind", "top", "bot", "h", "顶缝", "顶余", "底缝", "底余",
            "切穿顶", "切穿底", "右贴边", "flags"]
    with (out / "qa.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()})

    bad = [r for r in rows if r["flags"]]
    md = ["# 切点质检", "", "共 %d 个裁剪片（%d 题），有标 %d 片。" % (len(rows), len(questions), len(bad)), ""]
    if bad:
        md += ["| 题 | 页 | 类型 | 高 | 顶缝 | 底缝 | 标记 |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in bad:
            md.append("| %s | p%d | %s | %.0fpt | %.1f | %.1f | %s |"
                      % (r["id"], r["page"], r["kind"], r["h"], r["顶缝"], r["底缝"], "、".join(r["flags"])))
    else:
        md.append("全部干净：没有窄缝、没有切穿、没有贴边。这张表看完就可以交付，不用点开任何图。")
    md += ["", "联系表：联系表.png（%d 张缩略图，红框=有标）" % n_thumbs]
    (out / "qa.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return {"rows": rows, "flagged": len(bad)}


if __name__ == "__main__":
    a = sys.argv
    if len(a) < 2:
        raise SystemExit("用法: python qa.py <原书.pdf> [config.yaml] [输出目录]")
    main(a[1], a[2] if len(a) > 2 else None, a[3] if len(a) > 3 else None)
