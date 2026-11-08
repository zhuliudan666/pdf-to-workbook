"""体检：动手切题之前，先看清这本 PDF 的底细，并给出一个明确的结论。

输出 probe.json + probe.md，除了底细，还要回答一个脚本不会回答的问题：
**这本该不该用这套流水线做？** 结论分三档，附理由和推荐路线。

  1. 有没有文本层（没有就只能走 OCR 路线，切题正则全部失效）
  2. 哪条题号正则命中率最高（决定 config 里放哪条）
  3. 页眉页脚长什么样（决定 ignore_patterns）
  4. 版面是不是单栏（切点算法假设单栏，双栏会整页切错）
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from statistics import median

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import common

# 判定阈值。都是经验值，改动前先想清楚为什么。
MIN_HITS_PER_PAGE = 0.5    # 题号正则平均每页命中不到半题，说明正则不适配这本排版
TWO_COL_RATIO = 0.28       # 左右两侧各自占的行比例都超过它，才进入双栏候选
GUTTER_RATIO = 0.75        # 中缝空白比例，只作诊断记录，不参与判定（见 spanning_lines）
MAX_IMAGES_PER_PAGE = 3.0  # 平均每页插图过多，题目可能以图片为主

# 「有没有文字层」不是一个是非题，是三个状态：
#   ok    原生文字层：坐标是排版算出来的，题号读出来就是原书的题号
#   ocr   扫描件 + 后来加的 OCR 层：**也能选中文字**，但坐标是认出来的、字也可能是错的
#   none  纯扫描件：一个字也选不中
# 中间这个状态最容易出事——它看起来和 ok 一模一样，只有翻原书才发现题号读错位了。
SCAN_COVER = 0.75          # 单张图盖住整页 3/4 以上 → 这页就是一整张扫描图
SCAN_COVER_WEAK = 0.5      # 半页以上，再配合下面任一信号才敢判
OCR_SPAN_CHARS = 6.0       # OCR 层常把一行拆成逐字/逐词的碎块
OCR_SPAN_STRICT = 3.0      # 极碎（平均不到 3 个字一个 span）几乎只能是 OCR
OCR_FONTS = 2              # OCR 层通常只嵌一两种字体
OCR_NONEMBEDDED = 0.5      # 且多半没嵌入（GlyphLessFont 之类）


def classify_text_layer(chars_per_page: float, image_cover: float = 0.0,
                        chars_per_span: float = 99.0, font_count: int = 99,
                        nonembedded_ratio: float = 0.0) -> str:
    """三态判据。写成纯函数是为了能在自检里钉住——它判错的时候不会报错，
    只会让整本书走错路线，然后安静地交付。」"""
    if chars_per_page < 80:
        return "none"
    # 一页整张图 → 上面的字不可能是排版排上去的，只可能是后加的
    if image_cover >= SCAN_COVER:
        return "ocr"
    if image_cover >= SCAN_COVER_WEAK and (chars_per_span <= OCR_SPAN_CHARS
                                           or nonembedded_ratio >= OCR_NONEMBEDDED):
        return "ocr"
    if (chars_per_span <= OCR_SPAN_STRICT and font_count <= OCR_FONTS
            and nonembedded_ratio >= OCR_NONEMBEDDED):
        return "ocr"
    return "ok"


def text_signals(doc, sample: int = 12) -> dict:
    """采三个信号：整页图占比、每 span 平均字数、字体种数与未嵌入比例。

    都取中位数/计数，不取平均：封面、目录、插页跟正文差别很大，
    平均值会被那几页带跑，而这些页恰恰不是判断正文性质的依据。
    """
    covers: list[float] = []
    spans: list[float] = []
    fonts: Counter = Counter()
    ext: Counter = Counter()
    step = max(1, len(doc) // max(1, sample))
    for i in range(0, len(doc), step):
        page = doc[i]
        area = page.rect.width * page.rect.height or 1.0
        cover = 0.0
        for info in page.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            cover = max(cover, max(0.0, x1 - x0) * max(0.0, y1 - y0) / area)
        covers.append(cover)

        chars = n_spans = 0
        for blk in page.get_text("rawdict")["blocks"]:
            for line in blk.get("lines", []):
                for span in line["spans"]:
                    n_spans += 1
                    chars += len(span.get("chars") or [])
        if n_spans:
            spans.append(chars / n_spans)

        for f in page.get_fonts(full=True):
            fonts[str(f[3])] += 1        # basefont
            ext[str(f[1])] += 1          # ext: ttf/cff/n/a

    total_ext = sum(ext.values()) or 1
    return {
        "image_cover": round(median(covers), 2) if covers else 0.0,
        "chars_per_span": round(median(spans), 1) if spans else 0.0,
        "font_count": len(fonts),
        "nonembedded_ratio": round((ext.get("n/a", 0) + ext.get("", 0)) / total_ext, 2),
    }


def layout_split(page) -> tuple[int, int, int]:
    """返回 (左侧行数, 右侧行数, 总行数)，用来判断单栏还是双栏。

    判据故意宽松：只看 x0 落点，不看 x1。双栏书里栏间空白一般在版心中部，
    所以右侧行的 x0 会明显超过页宽一半。
    """
    w = page.rect.width
    mid = w * 0.5
    left = right = total = 0
    for ln in common.page_lines(page):
        x0 = ln.get("x0", 0)
        total += 1
        if x0 < mid * 0.55:
            left += 1
        elif x0 > mid:
            right += 1
    return left, right, total


def spanning_lines(page) -> int:
    """有多少行「横穿版心中线」。真双栏这一项必然接近 0。

    这是判双栏最硬的一条。x0 落点会被「四选项排成 A/B、C/D 两列」打穿
    （中文试卷基本都这么排，右半起步的行轻松过半），gutter 也只在 0.75–0.85 之间摇摆，
    跟真双栏的 1.00 分不开。但「有没有一行从左边一直写到右边」是结构性的：
    双栏的栏间空白不允许任何正文行穿过去，而单栏的题干、章节标题天然横穿。
    实测四份样本：真双栏 0 行，2024 数三三页分别 7 / 15 / 6 行。
    留 1 行的余量给页码、中线规则线这类贯通元素。
    """
    mid = page.rect.width / 2
    n = 0
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            x0, _, x1, _ = ln["bbox"]
            if x0 < mid - 20 and x1 > mid + 20:
                n += 1
    return n


def gutter_ratio(page, band_pt: float = 8.0, dpi: int = 110) -> float:
    """版心中部那条竖缝，在正文行里有多「空」。返回 0–1 的比例。

    为什么不能只看 x0 落点：中文试卷的四个选项按 A/B、C/D 排成两列，
    右半起步的行轻松过半，`layout_split` 会把每一页都判成双栏——2024 数三
    三页全部中招，probe 直接给出 NO-GO，而它其实是标准单栏。

    区别在于：真双栏有一条从上贯到下的空白缝，正文几乎每一行在那里都是空的；
    两列选项只是**局部**排成两列，题干、章节标题照样横穿版心。
    所以这里量的是「有墨的行里，中缝也是空的比例」，跟 x0 判据取交集才判双栏。
    """
    import numpy as np

    pm = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    a = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width)
    ink = a < 200
    h, w = ink.shape
    body = ink[int(h * 0.08):int(h * 0.92)]          # 掐掉页眉页脚，它们整行贯通是正常的
    rows = body.sum(axis=1) > 0
    n = int(rows.sum())
    if n < 10:
        return 0.0
    half = max(1, int(band_pt * dpi / 72 / 2))
    mid = body[:, int(w / 2) - half:int(w / 2) + half]
    return float(((mid.sum(axis=1) == 0) & rows).sum() / n)


def is_two_column(page) -> tuple[bool, dict]:
    """判一页是不是双栏，并把量到的东西一并返回（给诊断看）。

    判据是两条的交集：
      1. 左右两侧起步的行都超过 28%（`layout_split`）——只是候选；
      2. 横穿版心的行 ≤ 1（`spanning_lines`）——真正的判据。
    第 2 条是为了不把「四选项排成 A/B、C/D 两列」的中文试卷判成双栏：
    那种版面第 1 条必然满足，但题干、章节标题是横穿版心的。
    """
    left, right, tot = layout_split(page)
    split = tot >= 10 and left / tot > TWO_COL_RATIO and right / tot > TWO_COL_RATIO
    span = spanning_lines(page) if split else 0
    gutter = gutter_ratio(page) if split else 0.0
    return bool(split and span <= 1), {
        "left": round(left / max(tot, 1), 2),
        "right": round(right / max(tot, 1), 2),
        "lines": tot,
        "spanning": span,
        "gutter": round(gutter, 2),
    }


def judge(report, two_col_pages, hits_per_page, img_per_page) -> dict:
    """给出 go / caution / no-go 的结论。这是本脚本唯一带判断力的部分。"""
    reasons, level, route = [], "go", "segment.py（有文本层，直接读题号坐标）"

    if report["text_layer"] == "none":
        level = "caution"
        route = "segment_ocr.py（无文本层，靠 OCR 找题号）"
        reasons.append(
            "没有文本层，是扫描件或纯图片 PDF。切题可行，但题号要靠 OCR 识别，"
            "比文本层路线慢一个数量级，且需要核对。"
        )

    if report["text_layer"] == "ocr":
        level = "caution"
        route = "segment_ocr.py（文字层是 OCR 加的，坐标和字都不可信）"
        reasons.append(
            "这本是**扫描件 + 后来加的 OCR 文字层**：文字能选中、也能搜，"
            "但坐标是识别出来的，字本身也可能认错（标题里的「考研」常被认成「老研」）。"
            "拿它读题号会让整册编号错位，而且每道题看起来都是好的。"
            "已改走条带 OCR，文字层只留给成品做检索影子。"
        )

    if two_col_pages:
        level = "no-go"
        route = "不建议直接跑。切点算法假设单栏，双栏会整页切错"
        reasons.append(
            f"{len(two_col_pages)} 页判定为双栏（左右两侧文字各占 {TWO_COL_RATIO:.0%} 以上）。"
            "本流水线的切点吸附按整页行墨量计算，双栏会把左右两栏的内容搅在同一行里，"
            "结果几乎必然出错。要么先把栏拆开再切，要么换方案。"
        )

    if report["text_layer"] == "ok" and hits_per_page < MIN_HITS_PER_PAGE:
        # 只在有文本层时看命中率。扫描件的字符数本来就是 0，
        # 拿正则在空字符串上跑必然全 0，这样判会误伤所有扫描件。
        level = "no-go" if level == "no-go" else "caution"
        reasons.append(
            f"题号正则平均每页只命中 {hits_per_page:.2f} 题，低于 {MIN_HITS_PER_PAGE}。"
            "先去 probe.md 的命中表里挑一条更贴合的正则，或者在 config.yaml 里补一条。"
            "正则不对的话，后面的切点算法再好也是在切错的位置。"
        )
    elif report["text_layer"] == "none":
        reasons.append(
            "扫描件的题号召回率无法在这里判断——正则跑在空文本上永远是 0。"
            "先拿 3 页跑一遍 segment_ocr.py，看 questions.json 的题号是否连续、有没有跳号，"
            "再决定要不要扩到全书。"
        )
    elif report["text_layer"] == "ocr":
        reasons.append(
            "OCR 层的题号命中率不能当准——字是认出来的，命中一条也可能把数字读错。"
            "以 segment_ocr 的题号序列和 structural 的连续性为准，不看这张命中表。"
        )

    if img_per_page > MAX_IMAGES_PER_PAGE:
        if level == "go":
            level = "caution"
        reasons.append(
            f"平均每页 {img_per_page:.1f} 张插图。如果题目本身以图片形式给出（截图题、图库题），"
            "切出来的题可能只有半截，需要在 crop 之后人工看图确认。"
        )

    if not reasons:
        reasons.append("文本层、单栏、题号命中率都正常，可以直接跑。")

    return {"level": level, "route": route, "reasons": reasons}


def main(pdf: str, cfg_path: str | None = None, out_dir: str | None = None) -> dict:
    cfg = common.load_config(cfg_path)
    doc = pymupdf.open(pdf)
    out = Path(out_dir or Path(pdf).parent / "workbook_out")
    out.mkdir(parents=True, exist_ok=True)

    report = {"pdf": str(Path(pdf).resolve()), "pages": len(doc), "detail": []}

    total_chars = 0
    total_images = 0
    two_col_pages = []
    pattern_hits = Counter()
    for pno, page in enumerate(doc):
        raw = page.get_text("text")
        chars = len("".join(raw.split()))
        total_chars += chars
        lines = common.page_lines(page)
        n_img = len(page.get_images(full=True))
        total_images += n_img
        two_col, col = is_two_column(page)
        if two_col:
            two_col_pages.append(pno + 1)
        report["detail"].append(
            {
                "page": pno + 1,
                "chars": chars,
                "lines": len(lines),
                "images": n_img,
                "two_col": two_col,
                "spanning": col["spanning"],
                "gutter": col["gutter"],
                "first_line": lines[0]["text"][:40] if lines else "",
                "last_line": lines[-1]["text"][:40] if lines else "",
            }
        )
        for pat in cfg["question_patterns"]:
            hits = common.find_marks(common.content_lines(page, cfg), {**cfg, "question_patterns": [pat]})
            pattern_hits[pat] += len(hits)

    # 「有没有文字层」是三态，不是一个布尔值。判错不会报错，只会让整本书走错路线：
    # 扫描件 + OCR 层会被当成电子版，题号读出来是个数字，只是不是原书的那个。
    chars_per_page = total_chars / max(len(doc), 1)
    sig = text_signals(doc)
    report["text_signals"] = sig
    report["text_layer"] = classify_text_layer(chars_per_page, **sig)
    scanned = report["text_layer"] != "ok"
    report["chars_per_page"] = round(chars_per_page, 1)
    report["pattern_hits"] = dict(pattern_hits)
    report["two_col_pages"] = two_col_pages
    report["images_per_page"] = round(total_images / max(len(doc), 1), 2)

    # 页眉页脚：在超过 40% 的页面上重复出现的首行/末行
    heads = Counter(r["first_line"] for r in report["detail"] if r["first_line"])
    tails = Counter(r["last_line"] for r in report["detail"] if r["last_line"])
    thresh = max(2, int(len(doc) * 0.4))
    report["repeated_heads"] = [t for t, c in heads.most_common(5) if c >= thresh]
    report["repeated_tails"] = [t for t, c in tails.most_common(5) if c >= thresh]

    best_hits = max(pattern_hits.values()) if pattern_hits else 0
    report["verdict"] = judge(report, two_col_pages, best_hits / max(len(doc), 1), report["images_per_page"])

    (out / "probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    v = report["verdict"]
    md = [f"# 体检报告：{Path(pdf).name}", ""]
    md.append(f"## 结论：{v['level'].upper()}")
    md.append("")
    md.append(f"- 推荐路线：{v['route']}")
    for r in v["reasons"]:
        md.append(f"- {r}")
    md.append("")
    md.append(f"- 页数：{report['pages']}")
    md.append(f"- 文本层：{report['text_layer']}（每页 {report['chars_per_page']} 字）")
    if report["text_layer"] == "none":
        md.append("")
        md.append("> 几乎没有文本层，大概率是扫描件。请走 OCR 路线或直接按页裁切。")
    elif report["text_layer"] == "ocr":
        md.append("")
        md.append("> 文字能选中，但这是**扫描件后面加的 OCR 层**：整页图占 "
                  f"{report['text_signals']['image_cover']:.0%}，平均每个文本块只有 "
                  f"{report['text_signals']['chars_per_span']:.1f} 个字。"
                  "坐标和字都不可信，题号别从它读。")
    if two_col_pages:
        md.append("")
        md.append(f"> 判定为双栏的页：{two_col_pages[:15]}")
    md.append("")
    md.append("## 题号正则命中数（挑最高的一条写进 config.yaml）")
    md.append("")
    md.append("| 命中 | 正则 |")
    md.append("| --- | --- |")
    for pat, cnt in pattern_hits.most_common():
        md.append(f"| {cnt} | `{pat}` |")
    md.append("")
    md.append("## 重复出现的页眉/页脚（建议加进 ignore_patterns）")
    md.append("")
    for t in report["repeated_heads"]:
        md.append(f"- 首行：`{t}`")
    for t in report["repeated_tails"]:
        md.append(f"- 末行：`{t}`")
    (out / "probe.md").write_text("\n".join(md), encoding="utf-8")

    print("\n".join(md))
    print(f"\n-> {out / 'probe.md'}")
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python probe.py <pdf> [config.yaml] [输出目录]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
