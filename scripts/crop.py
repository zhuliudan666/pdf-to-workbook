"""把每道题的矩形区域渲染成图片，裁掉四周多余空白。

关键取舍一：**横向只裁左沿，右沿留着，全书画布补到同一宽度**。
按行裁会毁掉题干缩进（缩进是排版语义：小问的悬挂缩进、公式的居中）；
但完全不裁也不行——同一本书奇偶页的版心差十几 pt，带大矩阵的行还会整行右移
二十几 pt，一张张贴出去左边就是锯齿。所以只把「本题所有墨迹的最小 x」减 8px 当左沿。
右沿不能跟着墨迹走：layout 是按版心宽度等比缩放每张图的，图宽不一，缩放比就不一，
上一题的字会比下一题大一圈。右沿保留、再统一补到全本最大宽度，字号才一致。
纵向必须裁，因为原书每题下面的留白宽度由原书决定，而做题本的留白由我们重新分配。

关键取舍二：**印刷体扫描件一律存 1-bit 二值图**。
题目是黑白印刷的线条画，不是照片。8 位灰度 PNG 每题要 138KB，二值后只要 6KB，
88 题从 12MB 掉到 0.5MB，而 300dpi 的二值点就是 1/300 英寸，印刷看不出差别。
（只有底色发黄、有水印的书才需要退回 image.mode=gray。）

关键取舍三：**顺手把 OCR 文字坐标归一化存下来**。
layout.py 用它在题图上面盖一层不可见文字，PDF 就能搜索/选中/复制题干，
而文件大小几乎不长——真正让体积爆掉的是位图，文字反而是零成本。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
import common

STACK_GAP = 24          # 跨页题两段之间的间距（像素）


def render_part(doc, part: dict, dpi: int) -> Image.Image:
    page = doc[part["page"]]
    clip = pymupdf.Rect(*part["rect"])
    pix = page.get_pixmap(clip=clip, dpi=dpi, colorspace=pymupdf.csGRAY)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def trim_bounds(img: Image.Image, thresh: int = 246, pad: int = 8) -> tuple[int, int]:
    """返回纵向要保留的区间 [top, bottom)。"""
    mask = img.point(lambda p: 255 if p < thresh else 0)
    bbox = mask.getbbox()
    if not bbox:
        return 0, img.height
    return max(0, bbox[1] - pad), min(img.height, bbox[3] + pad)


def ink_box(img: Image.Image, thresh: int = 246, min_col: int = 2) -> tuple[int, int] | None:
    """返回横向墨迹区间 [left, right)，没有墨就返回 None。

    thresh 要和二值化阈值（默认 200）一起改：扫描件边上常有 210-240 的浅灰影，
    按 246 判会把整张图判成「从 x=0 就有墨」，横向归一就白做了。

    min_col 是「一列至少几个墨点才算数」。扫描灰尘常常是 1px 单点或斜向两点
    （bbox 2×2 但每列只有 1 个墨点），一颗就能把左沿拉到远离正文的地方：
    实测例2.8 在 x=44 有颗斜向灰点，按「有没有墨」判，左沿从 94 掉到 44，
    整题右边白留 22pt、题号比别的题缩进一格。
    注意 min_col=2 只挡得住「每列 1 点」的灰；真遇到 ≥2 点高的竖条灰再往上调
    （69 张实测 2→3 左沿只差 1px，但右边细边会被多裁掉一截）。
    """
    import numpy as np

    mask = np.asarray(img.point(lambda p: 255 if p < thresh else 0)) > 0
    xs = np.flatnonzero(mask.sum(axis=0) >= max(1, min_col))
    if xs.size == 0:
        bbox = img.point(lambda p: 255 if p < thresh else 0).getbbox()
        return None if not bbox else (bbox[0], bbox[2])
    return int(xs[0]), int(xs[-1]) + 1


def ink_bands(mask, min_px: int = 2, rule_frac: float = 0.8) -> list[tuple[int, int]]:
    """把墨迹切成「行」：连续若干行有墨算一条带，(y0, y1)。

    mask 是 bool ndarray，True = 墨。

    两个坑：
    - 扫描灰尘一颗就能让整行「有墨」，所以一行至少要 min_px 个墨点才算。
    - 行列式竖线、矩阵大括号、表格竖线会**从上到下贯穿**，把所有行粘成一条带，
      分带直接失效（实测例1.1 的 292px 高的图被当成一条带，归左静默不生效）。
      所以先统计每列的墨量，把「墨量 ≥ rule_frac × 图高」的竖线列剔除掉**只用于分带**，
      算左沿时仍用原始 mask——竖线本身也是版面上的内容，不能当它不存在。
    """
    import numpy as np

    if mask.shape[0] < 8:
        return []
    cols = mask.sum(axis=0)
    rules = cols >= max(8, int(mask.shape[0] * rule_frac))
    body = mask.copy()
    body[:, rules] = False

    rows = body.sum(axis=1)
    rows = np.where(rows >= min_px, rows, 0)

    bands: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(rows):
        if v > 0:
            if start is None:
                start = i
        elif start is not None:
            bands.append((start, i))
            start = None
    if start is not None:
        bands.append((start, len(rows)))
    return bands


def flush_first_band(img: Image.Image, thresh: int = 246, max_shift_px: int = 250,
                     label_xy: tuple[float, float] | None = None) -> Image.Image:
    """把题号那一行的左沿推到和**本题正文左沿**齐。

    为什么需要：讲义为了安置大矩阵，会把「【例2.8】设 A= [...] , B= [...]」整行右移
    二三十 pt，正文（下一行的 r(ABC)=…）仍在版心左沿。一张张照原样贴出去，
    每道题的题号停在不同的 x 上，翻起来像没对齐（实测这本书左边参差 4–45pt）。

    **哪一行才是题号行**，不能猜「第一条墨带」：
    题号竖向居中排在多行矩阵/行列式左侧时，第一条墨带是矩阵的首行、题号落在
    第二三条带里。照「第一条」挪，会把行列式的第一行单独抽走，整块散架
    （例1.2 实测就是这样，图直接废了）。所以传 label_xy（题号在题图里的坐标）时，
    取「纵向罩住它的那条带」；不传（没有 OCR 坐标）才退回第一条。

    三条约束，少一条就会做坏：

    1. **只往左挪，不往右挪。** 本书题号本来就排在正文左沿右边 3–40pt，
       需要的一律是左移；右移意味着「这行的左沿比正文还靠左」，那是悬挂标点的
       正常排版，挪它反而会缩进错位（第一版做成双向，把 12 道本来就齐的题挪歪）。
    2. **对齐目标是本题其余行的最左沿，不是图片左沿。** 图片左沿是按整题墨迹算的，
       可能还在正文左边；对齐到图片左沿会把题号推到 x=0、正文留在 94px，
       看着比原来更歪（第二版就是这么错的）。
    3. **只动那一条带。** 其余行是小问的悬挂缩进，属排版语义，一律不碰。

    平移上限 max_shift_px（300dpi 下 250px ≈ 60pt）：再大就不是「给公式让位」，
    而是整行居中（比如一道题以居中的大公式开头），强行左对齐反而难看，宁可不碰。

    函数是幂等的：已经对齐过的图再来一遍，dx ≤ 0 直接原样返回。
    """
    import numpy as np

    mask = np.asarray(img.point(lambda p: 255 if p < thresh else 0)) > 0
    bands = ink_bands(mask)
    if len(bands) < 2:
        return img

    def band_left(y0: int, y1: int) -> int | None:
        # 一列至少 3 个墨点才算真笔画（300dpi 下竖笔宽 8px 以上），不误伤，滤掉灰点。
        xs = np.flatnonzero(mask[y0:y1].sum(axis=0) >= 3)
        return None if xs.size == 0 else int(xs[0])

    lefts = [band_left(y0, y1) for y0, y1 in bands]
    idx = 0
    if label_xy is not None:
        ly = float(label_xy[1])
        idx = None
        for i, (y0, y1) in enumerate(bands):
            if y0 - 6 <= ly <= y1 + 6:
                idx = i
                break
        if idx is None:
            # 题号正好落在两条带之间的空隙里：取纵向最近的那条。
            idx = min(range(len(bands)),
                      key=lambda i: min(abs(ly - bands[i][0]), abs(ly - bands[i][1])))
    if lefts[idx] is None:
        return img
    others = [lx for i, lx in enumerate(lefts) if i != idx and lx is not None]
    if not others:
        return img
    dx = lefts[idx] - min(others)
    if dx < 2 or dx > max_shift_px:
        return img
    top, bot = bands[idx]
    out = img.copy()
    strip = img.crop((lefts[idx], top, img.width, bot))
    out.paste(255, (0, top, img.width, bot))
    out.paste(strip, (lefts[idx] - dx, top))
    return out


def to_bitonal(img: Image.Image, threshold: int, despeckle: bool = True) -> Image.Image:
    """灰度 -> 纯黑白。阈值 200 是实测值：再低会吃掉浅笔画，再高会把扫描灰尘点成黑点。

    二值化会把原来淡淡的灰点变成纯黑，视觉上比灰度版更显脏，所以先过一道 3x3 中值：
    300dpi 下一个标点直径约 6px、笔画宽 8px 以上，而灰尘只有 1-2px，中值能把后者抹掉
    而不动字形。
    """
    if despeckle:
        img = img.filter(ImageFilter.MedianFilter(3))
    return img.point(lambda p: 255 if p > threshold else 0).convert("1")


def ink_pixels(img: Image.Image, thresh: int = 246) -> int:
    mask = img.convert("L").point(lambda p: 255 if p < thresh else 0)
    return sum(mask.histogram()[1:])


def page_ocr_lines(out: Path, page_no: int) -> list[dict]:
    f = out / "ocr" / f"page_{page_no:03d}.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("lines", [])
    except Exception:
        return []


def main(pdf: str, cfg_path: str | None = None, out_dir: str | None = None) -> list[dict]:
    cfg = common.load_config(cfg_path)
    doc = pymupdf.open(pdf)
    out = Path(out_dir or Path(pdf).parent / "workbook_out")
    crops_dir = out / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    questions = json.loads((out / "questions.json").read_text(encoding="utf-8"))
    dpi = int(cfg["paper"]["dpi"])
    img_cfg = cfg.get("image") or {}
    mode = img_cfg.get("mode", "bitonal")
    threshold = int(img_cfg.get("threshold", 200))
    want_text = bool(cfg.get("text_layer", True))

    staged: list[dict] = []
    for q in questions:
        parts: list[Image.Image] = []
        metas: list[dict] = []
        y = 0
        raws: list[tuple[Image.Image, dict]] = []
        for p in q["parts"]:
            raw = render_part(doc, p, dpi)
            top, bot = trim_bounds(raw)
            im = raw.crop((0, top, raw.width, bot))
            # 续页可能切成一条纯白，拼进答案区会在中间插一条白带
            if p.get("kind") == "cont" and ink_pixels(im) < 50:
                continue
            raws.append((raw, p))

        if not raws:
            raws = [(render_part(doc, q["parts"][0], dpi), q["parts"][0])]

        # 左沿取「本题所有分片的墨迹并集」再减 8px：题内的缩进原样保留，
        # 题与题之间的左边偏移归零（同一本书奇偶页版心差十几 pt，带大矩阵的行
        # 还会整行右移二十几 pt，不归零一张张贴出去就是锯齿）。
        # 右沿**不动**——横向只裁左不裁右，是因为 layout 会把每张图按版心宽度等比缩放：
        # 图宽不一，缩放比就不一，上一题的字会比下一题大一圈。右沿留着，各题宽度才一致。
        # 阈值必须传二值化那个（默认 200），不能用 ink_box 的默认 246：
        # 扫描件四周常有一圈 210-245 的浅灰影，按 246 判「有墨」，左沿永远算出 0，
        # 横向归零等于没做（这本 69 题里 60 多张图当时都是这么白跑的，
        # 表现在成品上就是奇数页的题号比偶数页往右缩 22pt，看着「没对齐」）。
        boxes = [b for b in (ink_box(raw, threshold) for raw, _ in raws) if b]
        left = max(0, min(b[0] for b in boxes) - 8) if boxes else 0

        for raw, p in raws:
            top, bot = trim_bounds(raw)
            im = raw.crop((min(left, raw.width), top, raw.width, bot))
            metas.append({"part": p, "top": top, "left": left, "y": y,
                          "raw_w": raw.width, "raw_h": raw.height})
            parts.append(im)
            y += im.height + STACK_GAP

        canvas_w = max(im.width for im in parts)
        canvas_h = sum(im.height for im in parts) + STACK_GAP * (len(parts) - 1)
        canvas = Image.new("L", (canvas_w, canvas_h), 255)
        for im, meta in zip(parts, metas):
            canvas.paste(im, (0, meta["y"]))

        # 题号那一行归左：原书为了给大矩阵让位常把它整行右移，
        # 一张张贴出去每题的题号停在不同的 x 上，看着没对齐。
        # 判定阈值必须和后面二值化用的那个一致（默认 200）：扫描件的底色常在
        # 230-245 之间，按 246 判「有墨」会把整张图当成一条墨带，归左悄悄失效。
        if (cfg.get("align") or {}).get("first_line_flush"):
            lxy = None
            if q.get("label_box") and metas:
                p0 = metas[0]["part"]
                bx, by = float(q["label_box"][0]), float(q["label_box"][1])
                seg = dpi / 72.0
                lxy = ((bx - p0["rect"][0]) * seg - left,
                       (by - p0["rect"][1]) * seg - metas[0]["top"])
            canvas = flush_first_band(canvas, thresh=threshold,
                                      max_shift_px=int(60 * dpi / 72.0), label_xy=lxy)

        staged.append({"q": q, "canvas": canvas, "metas": metas})

    # 全书画布统一到最大宽度（右侧补白）：layout 按宽度等比缩放，宽度一致
    # 才是同一个缩放比，字号才不会一题大一词小。
    book_w = max(s["canvas"].width for s in staged) if staged else 0
    for s in staged:
        q, canvas, metas = s["q"], s["canvas"], s["metas"]
        if canvas.width < book_w:
            padded = Image.new("L", (book_w, canvas.height), 255)
            padded.paste(canvas, (0, 0))
            canvas = padded
        canvas_w, canvas_h = canvas.width, canvas.height

        q["texts"] = collect_texts(out, metas, canvas_w, canvas_h, dpi) if want_text else []
        final = to_bitonal(canvas, threshold) if mode == "bitonal" else canvas
        name = f"{q['seq']:03d}_{common.safe_name(q['id'])}.png"
        final.save(crops_dir / name, optimize=True)
        q["image"] = str(crops_dir / name)
        q["width"], q["height"] = canvas_w, canvas_h

    (out / "questions.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total = sum(Path(q["image"]).stat().st_size for q in questions)
    print(f"裁切完成：{len(questions)} 题 -> {crops_dir}（{total / 1e6:.2f}MB，模式 {mode}）")
    return questions


def collect_texts(out: Path, metas: list[dict], cw: int, ch: int, dpi: int) -> list[dict]:
    """把整页 OCR 的行框映射到题图坐标系，归一化成 0-1，供 layout 盖不可见文字层。"""
    scale = dpi / 72.0
    items: list[dict] = []
    for meta in metas:
        p = meta["part"]
        x0, y0, x1, y1 = p["rect"]
        off = meta["y"] - meta["top"]
        # 横向裁过以后，题图左沿在原始像素里的位置是 meta["left"]，
        # 不减这一项，贴在题图上的文字层会整体右移到版心里去。
        xc = x0 + float(meta.get("left") or 0) / scale
        for ln in page_ocr_lines(out, p["page"] + 1):
            bx0, by0, bx1, by1 = ln["box"]
            text = (ln.get("text") or "").strip()
            if not text:
                continue
            # 只留真正是中文叙述的行。OCR 对公式的输出是"ln(1+α)"这种残渣，
            # 留在文字层里既不improve搜索，还会把整段复制文本弄脏。
            if sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff") < 2:
                continue
            cy, cx = (by0 + by1) / 2, (bx0 + bx1) / 2
            if not (y0 - 1 <= cy <= y1 + 1 and x0 - 2 <= cx <= x1 + 2):
                continue
            px0 = max(0.0, (bx0 - xc) * scale)
            px1 = max(0.0, (bx1 - xc) * scale)
            py0 = (by0 - y0) * scale + off
            py1 = (by1 - y0) * scale + off
            if py1 - py0 < 4 or px1 - px0 < 4:
                continue
            items.append(
                {
                    "t": text,
                    "u0": round(min(1.0, px0 / cw), 4),
                    "v0": round(max(0.0, min(1.0, py0 / ch)), 4),
                    "u1": round(min(1.0, px1 / cw), 4),
                    "v1": round(max(0.0, min(1.0, py1 / ch)), 4),
                }
            )
    return items


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python crop.py <pdf> [config.yaml] [输出目录]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
