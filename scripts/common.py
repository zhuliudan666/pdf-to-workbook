"""习题册 -> 做题本：共用工具层。

只做三件事：读配置、把页拆成"行"、按题号把行分成"题"。
其他脚本都从这里取数据，保证切题逻辑只有一份。
"""
from __future__ import annotations

import copy
import os
import re
import shutil
import sys
from pathlib import Path

import pymupdf

DEFAULTS = {
    "pages": None,
    "question_patterns": [
        r"^\s*(\d+)\s*[.．、]\s*[（(]\s*(\d+)\s*[)）]",
        r"^\s*(\d+)\s*[.．、]\s*(?=\S)",
        r"^\s*[（(]\s*(\d+)\s*[)）]\s*(?=\S)",
    ],
    "ignore_patterns": [
        r"^\s*B\s*站",
        r"第\s*\d+\s*页",
        r"^\s*[·•∙・]\s*\d+\s*[·•∙・]\s*$",
        # 试卷的「注意事项」是编号列表（1. 所有答题… / 2. 密封线…），
        # 会被题号正则当成第 1、2 题，于是整卷编号从 2 开始，后面全错位。
        r"注意事项",
        r"密封线",
        # 书脚那行分散成四五段（第1页／共3页／科目名称…），只忽略其中一段，
        # 剩下的会把「正文底」推到页脚上，末题的裁图就带上一条页脚残渣。
        r"^共\s*\d+\s*页",
        r"科目名称",
    ],
    "page_quota": {"选择题": 4, "填空题": 4, "解答题": 3, "证明题": 3},
    "page_quota_mixed": 3,
    "answer_gap_floor_by_section": {"选择题": 14, "填空题": 26, "解答题": 32, "证明题": 32},
    "answer_gap_floor_mm": 30,
    "answer_gap_cap_by_section": {"选择题": 40, "填空题": 60, "解答题": 130, "证明题": 130},
    "answer_gap_cap_mm": 120,
    "content_box": [50, 548],
    "ocr": {
        "strip_x0": 40,
        "strip_x1": 175,
        "strip_dpi": 300,
        "content_top": 40,
        "content_bottom_margin": 45,
    },
    "question_gap_mm": 6,
    "page_bottom_breath_mm": 6,
    "min_scale": 0.8,
    "gap_style": "blank",
    "image": {"mode": "bitonal", "threshold": 200},
    "text_layer": True,
    "paper": {                       # 版心边距，单位 pt（不是毫米；毫米字段都带 _mm 后缀）
        "size": "A4",
        "margin_top": 58,
        "margin_bottom": 46,
        "margin_left": 66,
        "margin_right": 46,
        "dpi": 300,
    },
    "header": {"book": "", "suffix": " · 做题本", "title": "", "show_source_page": True},
    "cross_page": {"merge_continuation": True},
}

PAPER_SIZES = {"A4": (595.28, 841.89), "A4L": (841.89, 595.28), "B5": (498.9, 708.7)}

MM = 72.0 / 25.4

CJK_FONTS = [
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux（Noto CJK 与文泉驿是发行版最常见的中文字体）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
]
# 章节标题要粗一档：正文题图是原书的印刷体，标题不加重的话，
# 「2. 一元函数微分学」看着和下面的题型标题一样重，层级就没了。
CJK_FONTS_BOLD = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
]
# 一条都找不到就别硬跑：页眉、章节标题、目录页码全是我们自己画的字，
# 没有中文字库时它们会静默变成空白或方块——成品看着"少了几行标题"，
# 却不会有任何报错。宁可在这里停一下。
_FONT_WARNED = False


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        p = Path(path)
        if p.exists():
            import yaml

            cfg = deep_merge(cfg, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        else:
            # 静默忽略路径错误是最坑的一种失败：配置没读进去，全套默认值照样跑，
            # 出来的是「整本 149 页切出 0 题」这种莫名其妙的结果，人只会去怀疑正则。
            # （踩过：从 Git Bash 传了 /d/xxx 这种虚路径，Python 侧根本不存在。）
            print(f"⚠️  配置文件不存在，已退回默认配置：{p}", file=sys.stderr)
    return cfg


_FONT_CACHE: dict[str, str | None] = {}


def cjk_font(bold: bool = False) -> str | None:
    """找一个真能嵌入的字体文件。.ttc 集合有时嵌不进去，所以真的试一遍。

    返回 None 时页眉、章节标题、目录页码都画不出来——所以这里会**出声**警告一次。
    静默返回 None 是这个函数以前最大的坑：成品少了几行标题，却没有任何报错，
    而"少标题"和"标题被切掉"在成品里长得几乎一样。
    """
    global _FONT_WARNED
    key = "bold" if bold else "path"
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    chosen = None
    for path in (CJK_FONTS_BOLD if bold else CJK_FONTS):
        if not Path(path).exists():
            continue
        try:
            scratch = pymupdf.open()
            page = scratch.new_page()
            page.insert_text((50, 50), "测试", fontname="cjk", fontfile=path, fontsize=10)
            scratch.close()
            chosen = path
            break
        except Exception:
            continue
    if chosen is None and not _FONT_WARNED:
        _FONT_WARNED = True
        print(
            "⚠ 找不到可用的中文字体，页眉/章节标题/目录页码会画不出来（成品里表现为"
            "整行标题消失，且不会报错）。\n"
            "  装一个中文字体即可：Windows 自带微软雅黑；macOS 自带苹方；"
            "Linux 装 fonts-noto-cjk 或 fonts-wqy-zenhei。\n"
            "  也可以把字体文件路径写进 common.CJK_FONTS 的最前面。",
            file=sys.stderr)
    _FONT_CACHE[key] = chosen
    return chosen


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_pages(spec, total: int) -> list[int]:
    """把 "1-9,12,20-" 这样的页范围解析成 1-based 页码列表。

    intake.py 生成的配置会带这个字段：用户只说"只做第 18 讲"时，
    讲从哪页起无法自动知道，最终仍要人给页码；两条切题路线都按它取页。
    """
    if not spec:
        return list(range(1, total + 1))
    if isinstance(spec, int):
        return [spec] if 1 <= spec <= total else []
    if isinstance(spec, (list, tuple)):
        return [int(p) for p in spec if 1 <= int(p) <= total]
    out: list[int] = []
    for chunk in str(spec).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            lo = int(a) if a.strip() else 1
            hi = int(b) if b.strip() else total
            out.extend(range(lo, min(hi, total) + 1))
        else:
            out.append(int(chunk))
    return [p for p in out if 1 <= p <= total]


def stamp_chapters(questions: list[dict], cfg: dict) -> int:
    """按页段给每道题盖「章」的章。

    为什么需要：文本层路线不产出 chapter —— 它只认题号，书里有没有「命题点」跟它无关。
    真题分类册每章从 1 重排，不给章，structural 就把全书的「第 1 题」当成同一题，
    一本讲义能报出几十条假重号（实测 4 章 × 5 题 = 5 条 high）。假高危比不报更坏：
    看得多了就没人再看这张表，真断了也发现不了。

    配置写法（pages 走 parse_pages，支持 "3-4,6"）：

        chapters:
          - title: 第1章 函数与极限
            pages: 3

    find_exercises.py 的 --patch 会直接把这段写进配置，不用手抄页码。
    """
    chaps = cfg.get("chapters") or []
    if not chaps:
        return 0
    if isinstance(chaps, dict):      # 也接受 {章名: 页码}
        chaps = [{"title": k, "pages": v} for k, v in chaps.items()]
    hi = max([q.get("source_page") or 0 for q in questions] + [1])
    page2chap: dict[int, str] = {}
    for c in chaps:
        title = str(c.get("title") or "").strip()
        if not title:
            continue
        for p in parse_pages(c.get("pages"), hi):
            page2chap.setdefault(p, title)
    if not page2chap:
        return 0
    n = 0
    for q in questions:
        t = page2chap.get(q.get("source_page") or 0)
        if t and not q.get("chapter"):
            q["chapter"] = t
            n += 1
    return n


SEC_RE = re.compile(
    r"^\s*[一二三四五六七八九十]+\s*[、.．]\s*(选择题|填空题|解答题|证明题|计算题|综合题)"
)

# 页首出现这些，就不是「上一题的续页」，是别人家的正文。
# 讲义整本跑时最典型：章末题的图还没收尾，下一页已经是「定义 …」「定理 3.19 …」；
# 再下一页开头又是「习题 3.1」。按位置判（上一题压到页底）三条全中，
# 结果是每道章末题都背着整页正文，看着像题很长，其实印的是别人的课文。
# 续页永远是句子的下半截、公式、选项——不会以「第3章」「习题 3.1」「定义」开头。
# 关键词后面必须跟分隔符或数字才算——「定义 连续…」「定理 3.19」命中，
# 「解法如下」「证明过程见上」不命中。不用 \b：中文是 \w，\b 在「定义连」之间不成立。
HEAD_STOP_RE = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十百零〇\d]+\s*[章讲节]|Chapter\s*\d+"
    r"|课后习题|习题|练习|练习题|思考题|Exercises|Problems"
    r"|定义|定理|引理|推论|性质|命题|例|证明|证|解|注|图|表|公式)"
    r"(?=\s|\d|$|[：:、，,。．])",
    re.I,
)


def sections_in(lines: list[dict]) -> list[tuple[float, str]]:
    """页内章节标题行（「一、选择题」这种），返回 (y0, 章节名)。

    留白按题型分档（选择 18 / 填空 33 / 解答 62mm），分档的键就是这里认出的章节名。
    文本层路线原来不产出 section，导致分档对它失效、只能吃 fallback 值。
    """
    out = [(ln["y0"], SEC_RE.match(ln["text"]).group(1)) for ln in lines if SEC_RE.match(ln["text"])]
    out.sort()
    return out


def page_lines(page) -> list[dict]:
    """一页里的所有文本行，按从上到下、从左到右排序。"""
    lines = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if not text.strip():
                continue
            x0, y0, x1, y1 = line["bbox"]
            lines.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": norm(text)})
    lines.sort(key=lambda r: (round(r["y0"], 1), r["x0"]))
    return lines


def content_lines(page, cfg) -> list[dict]:
    """正文行：剔掉页眉页脚水印、广告、页码。

    水印经常**不是一行**：左边「公众号：xxx」、右边「26 考研某某四套卷数三」在
    PDF 里是同一水平带上的两段文字，只按行首词剔会漏掉右半截。漏掉的那半截
    会当成正文，把整条带子顶到页面最上面，裁切时连广告墨迹一起框进题图
    （实测第 6 题续到下一页的残条里带着「公众号：风中羽小易」整行）。

    所以按**水平带**判：同一条带（y 重叠过半）里只要有一段命中忽略规则，
    整条带都算水印。带题号的行例外——它永远不算水印，免得把题切没了。
    """
    pats = cfg["ignore_patterns"]
    qpats = cfg["question_patterns"]
    lines = page_lines(page)
    if not pats:
        return lines
    hits = [l for l in lines if any(re.search(p, l["text"]) for p in pats)]
    if not hits:
        return lines

    def same_band(a: dict, b: dict) -> bool:
        overlap = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
        short = min(a["y1"] - a["y0"], b["y1"] - b["y0"]) or 1.0
        return overlap >= 0.5 * short

    keep = []
    for l in lines:
        if any(re.search(p, l["text"]) for p in pats):
            continue                      # 自己就是水印
        if any(re.match(p, l["text"]) for p in qpats):
            keep.append(l)                # 题号行绝不因"同带"被删
            continue
        if any(same_band(l, h) for h in hits):
            continue                      # 和水印同一条带：算水印的另一段
        keep.append(l)
    return keep


def find_marks(lines: list[dict], cfg) -> list[dict]:
    """在行里找题号起点。"""
    pats = [re.compile(p) for p in cfg["question_patterns"]]
    marks = []
    for line in lines:
        for pat in pats:
            m = pat.match(line["text"])
            if m:
                marks.append(
                    {
                        "y0": line["y0"],
                        "y1": line["y1"],
                        "x0": line["x0"],
                        "label": norm(m.group(0)),
                        "text": line["text"][:60],
                    }
                )
                break
    marks.sort(key=lambda r: r["y0"])
    deduped = []
    for mk in marks:
        if deduped and abs(mk["y0"] - deduped[-1]["y0"]) < 6:
            continue
        deduped.append(mk)
    return _drop_floating_numbers(deduped)


def _drop_floating_numbers(marks: list[dict], tol: float = 8.0) -> list[dict]:
    """去掉「题号独立成行、但不在左边距」的假题号。

    为什么需要：题号后面紧跟一个大公式时，PDF 会把「12.」单独排成一行，
    行里没有别的字，`(?=\\S)` 那个断言就失效了，于是要补一条 `^\\s*(\\d+)\\s*[.．、]\\s*$`。
    但补上之后，公式里的分子 `12` 也会作为独立行出现——2024 数三第 20 题的
    分式 1/12，那个「12.」落在 x≈259，被当成了第 12 题，结果整册编号错位。

    判据：真题号贴着正文左边距排（这本是 93.6pt），公式碎片在版心内部。
    参照左边距取「有正文的题号」的 x0 中位数，而不是整页最小 x0——
    后者会被页眉、页码这类贴边元素拉歪。
    """
    body = [m for m in marks if m["text"].strip() != m["label"]]
    if not body:
        # 整页题号都独立成行（每题后面都跟大公式）时无从判断，宁可不动。
        # 注意这里**不能**要求 body ≥3：find_marks 是逐页调用的，
        # 2024 数三第 3 页只有 21、22 两个正常题号，按 3 条设门槛整页都放过，
        # 那个落在 x=259 的分式分子又混进来了。
        return marks
    ref = sorted(m["x0"] for m in body)[len(body) // 2]
    return [m for m in marks
            if m["text"].strip() != m["label"] or m["x0"] <= ref + tol]


def content_xrange(doc, cfg) -> tuple[float, float]:
    """整本书的正文左右边界。

    左边界取各页的众数（题号都从同一条线起头）。右边界取**最宽**那一页，
    不再取众数：正文里偶尔会有一行溢出到页边距外——2024 数三第 22 题的
    第一行 x1=532.8，比正文右界宽 8pt，按中位数取就把这行尾巴裁掉了
    （"X_1, X_2, ⋯, X_n" 只剩下半个 X）。右边界放宽只是多几点白边，
    裁掉字却是硬伤。放宽超过页宽 6% 就当离群值，退回众数。
    """
    lefts, rights, width = [], [], 595.0
    for page in doc:
        lines = content_lines(page, cfg)
        width = page.rect.width
        if not lines:
            continue
        lefts.append(min(l["x0"] for l in lines))
        rights.append(max(l["x1"] for l in lines))
    if not lefts:
        return (72.0, 523.0)
    lefts.sort()
    rights.sort()
    mid = len(lefts) // 2
    right = max(rights)
    if right > rights[mid] + 0.06 * width:
        right = rights[mid]
    # 再放 2pt 白边：最宽那一行正好压在裁剪边上，印出来右边一线笔画容易被裁掉，
    # 而且 qa.py 会把"右贴边"标出来。2pt 不到 1mm，页面缩放的差别看不出来。
    return (lefts[mid], right + 2.0)


def _row_ink(page, dpi: int = 150) -> "object":
    """整页逐行的墨量（每行有多少像素比 150 灰阶更黑）。"""
    import numpy as np

    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return (arr < 150).sum(axis=1), dpi / 72.0


def ink_blocks(page, dpi: int = 150, ink: int = 0, min_h: float = 0.3) -> list[tuple[float, float]]:
    """整行墨迹块（上下界，pt）：连续有墨的那些行连成一块。

    ink 门槛是「这一行最多容忍几个墨点」，默认 0——有一个墨点就算有墨。
    以前取 1/2，看的是「这行是不是这一页里写得比较满的一行」；但在 150dpi 下
    一个小号字（上限符号、分母）的竖笔一行只有 1 个墨点，被判成空白，一块墨
    被切成两截，切点就落进字里。0 才是"空行"的本义：整行纯白。
    """
    rows, sc = _row_ink(page, dpi)
    out, start = [], None
    for i, v in enumerate(rows):
        if v > ink:
            if start is None:
                start = i
        elif start is not None:
            if (i - start) / sc >= min_h:
                out.append((start / sc, i / sc))
            start = None
    if start is not None and (len(rows) - start) / sc >= min_h:
        out.append((start / sc, len(rows) / sc))
    return out


def merge_slivers(blocks: list[tuple[float, float]],
                  join_pt: float = 8.0, ratio: float = 0.5) -> list[tuple[float, float]]:
    """把「薄片」并进相邻的整行，再拿并完的块去求空白带。

    上限符号不是独立的行：∑ 上方的 ∞、∫ 上方的 π/6 只比下面的主线高 2–3pt，
    高度也只有正文行的三分之一。按「最近的空白带」切，切点会落在这条 3pt 的缝里，
    那个 ∞ 就留在上一题的屁股上（2024 数三第 3/4 题：第 3 题选项下面孤零零一个 ∞）。
    这类薄片必须并进它所属的那一行，缝消失了，切点自然上移到真正的题间距。

    并入方向：优先并给**下面**那一块（上标属于它下面的主线）；下面太远就并给上面
    （下标、脚注属于上面的行）。join_pt=8 是上限：更远的薄片就是独立的小标记了。
    """
    if not blocks:
        return []
    hs = sorted(b - a for a, b in blocks)
    thin_h = max(2.4, ratio * hs[len(hs) // 2])
    out: list[list[float]] = []
    i, n = 0, len(blocks)
    while i < n:
        a, b = blocks[i]
        if (b - a) < thin_h:
            # 先看下面：上标属于它下面那一行，并进去以后那条 3pt 的缝就没了
            j = i + 1
            while j < n and blocks[j][0] - b <= join_pt and (blocks[j][1] - blocks[j][0]) < thin_h:
                b = blocks[j][1]
                j += 1
            if j < n and blocks[j][0] - b <= join_pt:
                out.append([a, blocks[j][1]])
                i = j + 1
                continue
            # 下面太远，退回并给上面（下标、脚注属于上面的行）
            if out and a - out[-1][1] <= join_pt:
                out[-1][1] = b
                i = j
                continue
            out.append([a, b])
            i = j
            continue
        out.append([a, b])
        i += 1
    merged: list[list[float]] = []
    for blk in out:
        if merged and blk[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], blk[1])
        else:
            merged.append(blk)
    return [(a, b) for a, b in merged]


def ink_gaps(page, dpi: int = 150, min_gap_pt: float = 1.2, ink: int = 0,
             blocks: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    """整行空白带（上下界，pt）——墨迹块的补集。

    题号的文字框顶**不是**题目的上边缘：紧跟题号的大积分号、分式会向上顶出十几点，
    而下一题的分式底又会垂到自己题号线以下。按「下一题题号 −2pt」硬切的结果是
    两头都难看：上一题底部挂着下一题积分号的残渣，下一题顶部又把积分上限削掉一截
    （2024 数三第 11/12 题实测各削 5–7pt）。吸到整行空白带上就没有这个问题。
    门槛 1.2pt 是刻意的低值：密排的填空题题间距常常只有 2–3 点。
    """
    blocks = merge_slivers(ink_blocks(page, dpi=dpi, ink=ink)) if blocks is None else blocks
    gaps, cur = [], 0.0
    for a, b in blocks:
        if a - cur >= min_gap_pt:
            gaps.append((cur, a))
        cur = max(cur, b)
    height = page.rect.height
    if height - cur >= min_gap_pt:
        gaps.append((cur, height))
    return gaps


def ink_floor(blocks: list[tuple[float, float]], y: float, limit: float,
              reach: float = 6.0) -> float:
    """文字框底下面可能还垂着墨：矩阵的括号、积分号的下限、分式的分母。

    2024 数三第 6 题页底那排分块矩阵比文字框低 0.66pt；跨年切片里那道跨页题，
    续文最后一行文字框底在 216.3，而它所在的那块墨一直到 225.1（一个大积分）。
    按文字框 +2 切，最后一行字就被削掉一条——打印看不出来，放大就看出来了。

    先把「文字框底落在哪块墨里」找出来直接用那块的下界（这是最常见的情况），
    再退一步认 y 下面 reach 内的墨（文字框底正好落在缝里时）。
    """
    for a, b in blocks:
        if a - 0.5 <= y <= b:
            return min(b, limit)
    near = [b for a, b in blocks if y < b <= min(limit, y + reach)]
    return max(near) if near else y


def snap_cut(y: float, gaps: list[tuple[float, float]], fallback: float,
             back: float = 22.0, blocks: list[tuple[float, float]] | None = None) -> float:
    """把切点吸附到 y 附近最靠下的那条空白带中心。

    窗口是 [y-back, y+4]：上界给到 +4 是因为文字框顶与上方空白带下界常只差半个点，
    卡死在 y-6 会把唯一正确的那条排掉；取最靠下而不是最宽，是因为页内行距可能
    比题间距还宽，按最宽选会切进题目内部。

    下界原先只判「与窗口相交」，结果页顶那条 74pt 的大空白（页边距）也算候选：
    第 2 页第一题切到了 y=68.5，把上一题残留的矩阵底整块划进了下一题。
    现在要求**空白带中心**落在窗口内，页边距那种半页开外的直接不算。

    back 默认 22pt，但题目的顶边要放宽到 34：三行矩阵的第一行常常比题号高
    十几点（2024 数三第 6 题的 P^T A P^2 就是，按 22pt 找会把第一行切掉）。

    一条候选都没有时（题号被上面的公式"粘"住了，整块墨从上往下不断），
    fallback（=题号顶 −2pt）会正好落在墨迹中间，把这道题自己的公式削掉一截。
    传了 blocks 就改判：宁可退到那一整块墨的上方空白带，也不切在墨里
    （2024 数三第 8 题：题号 192.98，墨块从 185.28 就连上了）。
    """
    cand = [g for g in gaps if y - back <= (g[0] + g[1]) / 2.0 <= y + 4.0]
    if cand:
        g = max(cand, key=lambda t: t[1])
        return (g[0] + g[1]) / 2.0
    if blocks:
        b = next((bl for bl in blocks if bl[0] < fallback < bl[1]), None)
        if b is not None and b[0] <= y - 3.0:
            above = next((g for g in gaps if abs(g[1] - b[0]) < 0.6), None)
            # 这块墨就是版心第一块时，上面那条"缝"是页边距（几十点高），
            # 把切点挪到页边距正中等于把整页上方都划进这道题。这种情况贴着墨顶切。
            if above is not None and above[1] - above[0] <= 30.0:
                return (above[0] + above[1]) / 2.0
            return b[0] - 1.0
    return fallback


def build_questions(doc, cfg) -> list[dict]:
    """把整本书切成题。跨页的题合并成 parts 列表。

    `cfg["pages"]` 可以限定只做其中几页（对应"只做第 18 讲"这类需求）。
    注意跨页合并只在被选中的相邻页之间生效——选不连续的页时，
    页首残段会被当成正文的开头，这是刻意的：宁可不合并，也不要跨过
    几十页去接一段无关内容。
    """
    keep = set(parse_pages(cfg.get("pages"), len(doc)))
    pages = []
    for pno, page in enumerate(doc):
        if pno + 1 not in keep:
            continue
        lines = content_lines(page, cfg)
        pages.append(
            {"pno": pno, "lines": lines, "marks": find_marks(lines, cfg)}
        )

    x0, x1 = content_xrange(doc, cfg)
    questions: list[dict] = []
    cur_sec = None

    # 「答案从这里开始」的行也算切点。例题的题干和解答紧挨着印，
    # 只在下一个题号处切，题图里就带着解答——做题本上答案已经写在题目下面，
    # 这本书就废了。默认不开：真题册不会在题干后面直接跟「解：」，
    # 万一有（比如「解方程」开头），误切比漏切难查。例题路线由 intake 显式打开。
    stop_pats = [re.compile(s) for s in (cfg.get("cut_stop_patterns") or [])]

    for p in pages:
        lines = p["lines"]
        if not lines:
            continue
        pno = p["pno"]
        page = doc[pno]
        marks = p["marks"]
        heads = sections_in(lines)
        stops = sorted([hy for hy, _ in heads]
                       + [ln["y0"] for ln in lines
                          if stop_pats and any(rx.match(ln["text"]) for rx in stop_pats)])
        raw = ink_blocks(page)
        blocks = merge_slivers(raw) if marks else []
        gaps = ink_gaps(page, blocks=blocks) if marks else []

        # 页底不能用「最后一行文字框底 +2」：矩阵的括号、积分号的下限会垂到文字框
        # 下面零点几到几点（2024 数三第 6 题，页底那一排分块矩阵垂 0.66pt），
        # 按文字框切就把最后一行字的底边削掉一条。
        bottom = ink_floor(raw, max(l["y1"] for l in lines), page.rect.height) + 2
        top = min(l["y0"] for l in lines) - 2

        def sec_at(y: float) -> str | None:
            got = None
            for hy, name in heads:
                if hy <= y + 1:
                    got = name
                else:
                    break
            return got

        # 页首、第一个题号之前的内容：几乎一定是上一题的续页。
        # 但章节标题（「二、填空题」）不属于任何一道题——它由 layout.py 自己画，
        # 留着会变成一张只有标题的假题目（id 形如 p1-页首）。所以先把标题行剔掉，
        # 只看剩下的内容：没有内容就整段跳过。
        # 每题的上下边界只算一次，相邻两题共用同一条：上一题的下界就是下一题的上界。
        # 各自算一遍会出现「上一题切到 380、下一题从 376 起」的重叠，
        # 两张图都带上对方的残渣。
        cuts = []
        for mk in marks:
            c = snap_cut(mk["y0"], gaps, mk["y0"] - 2, back=34.0, blocks=blocks)
            # 题号的**文字框**顶不等于墨的顶：中文行加数学符号，行高留白常有三四点。
            # 以前这里按「不超过题号框顶 −2pt」封顶，遇上框顶比墨顶还低的情形
            # （第 19 题框顶 700.33、墨顶 703.68），唯一正确的那条 5pt 缝被封到
            # 698.33，正好切在上一题尾笔上，第 19 题头顶就挂了一小截余墨。
            # 改成按**题号所在墨块**的顶来封顶：缝在墨块上方就一定不切到字。
            blk = next((b for b in blocks if b[1] > mk["y0"]), None)
            lim = blk[0] - 0.5 if blk is not None else mk["y0"] - 2
            cuts.append(max(top, min(c, lim)))
        for i in range(1, len(cuts)):
            if cuts[i] < cuts[i - 1] + 3:
                cuts[i] = cuts[i - 1] + 3

        head_end = cuts[0] if cuts else bottom
        head_body = [l for l in lines if top <= l["y0"] < head_end and not SEC_RE.match(l["text"])]
        # 页首这块也可能根本不是「上一题的续页」，而是**下一题自己的公式顶出来的**：
        # 第 2 页顶部是一整块矩阵，它的位置比「7.」这个题号还高十几点。
        # 判据不能只看文字框跨过题号——中文行加数学符号时，一个高积分号的框底
        # 本来就常垂到下面一行（第 3 页顶那道证明题的第二问就是），按框判会把
        # 上一题的第二问整段划给下一题。改成按**墨**判：题号被"粘"在一块比正文行
        # 高得多的墨里，且这块墨从题号上方一大截就开始了，那才是同一道题的公式。
        straddle = False
        if head_body and marks:
            y0 = marks[0]["y0"]
            hs = sorted(b - a for a, b in blocks)
            med = hs[len(hs) // 2] if hs else 10.0
            straddle = any(b[0] < y0 - 5.0 and b[1] > y0 + 2.0 and (b[1] - b[0]) > 1.8 * med
                           for b in blocks)
        if straddle:
            cuts[0] = min(cuts[0], min(l["y0"] for l in head_body) - 2)
            head_body = []
        if head_body:
            head_top = min(l["y0"] for l in head_body) - 2
            head_bot = min(head_end, ink_floor(raw, max(l["y1"] for l in head_body), head_end) + 2)
        else:
            head_top = head_bot = 0
        # 页首这一带的净高门槛：低于半行字（≈8pt）的不可能是"上一题的下半截"，
        # 只可能是被 ignore_patterns 剔掉的水印/广告残留的墨迹，或者 content_top
        # 压在一条横线上的半个像素。以前门槛是 3pt，一本四套卷合订实测在这种带上
        # 生成了 3.54pt 的「空续片」，结构校验报「题区间过窄」——真续页至少是一整行字。
        if head_body and head_bot - head_top > 8:
            # 「续页」必须是**物理上紧挨着的下一页**。pages 允许离散页段
            # （讲义只做每章课后题时常写 "3,6,9,12"），这时 p3 的下一张选中页是 p6，
            # 中间隔着一整章的正文。以前只看「上一个选中页」，结果每章最后一题
            # 都把下一章第一页的「定义/定理 3.19 …」接成了续页：切出来的图是
            # 本题加一整页正文，看着像题很长，实际是别人的正文。
            prev_pg = -99
            if questions and questions[-1].get("parts"):
                prev_pg = questions[-1]["parts"][-1].get("page", -99)
            adjacent = prev_pg == pno - 1
            # 光位置近不够，还得看内容：页首第一行是「定义/定理/第3章/习题 3.1」，
            # 那它是别人家的正文开头，不是上一题的下半截。整本跑讲义时三种标记
            # 会依次出现（章末题 → 下一页正文 → 再下一页「习题 x.1」），
            # 只按位置判就一路接下去，一道题最后背着两页课文。
            other_body = bool(head_body) and bool(HEAD_STOP_RE.match(head_body[0]["text"]))
            if cfg["cross_page"]["merge_continuation"] and questions and adjacent and not other_body:
                questions[-1]["parts"].append(
                    {"page": pno, "rect": [x0, head_top, x1, head_bot], "kind": "cont"}
                )
                questions[-1]["span_pages"] += 1
            elif questions and (not adjacent or other_body):
                # 两种情况都丢掉，且不要建成「页首残段」：不连续的页段、以及页首是
                # 「定义/定理/第X章/习题」这种别人家正文的开头——它不是哪道题的前半截，
                # 建成残段会在做题本里多出一道只有课文的假题。
                pass
            elif not questions:
                # 选区第一页的页首不可能是「上一题的续页」——它前面没有题。
                # 这段内容要么是封面/试卷抬头（题号、满分、得分表、注意事项），
                # 要么是用户从半道题开始做时那道题的前半截。两种都不该进做题本：
                # 前者印出来是一张只有表格的假题目，后者是一道缺了题号的残题。
                pass
            else:
                questions.append(
                    {
                        "id": f"p{pno + 1}-页首",
                        "seq": len(questions) + 1,
                        "source_page": pno + 1,
                        "span_pages": 1,
                        "section": cur_sec,
                        "parts": [{"page": pno, "rect": [x0, head_top, x1, head_bot], "kind": "orphan"}],
                    }
                )

        for mi, mk in enumerate(marks):
            # 切点已在 cuts 里吸附到整行空白带：题号框顶不是题目的上边缘，
            # 积分号、分式会往上顶，而上一题的分式底又会垂到自己题号线以下。
            seg_top = cuts[mi]
            seg_bot = cuts[mi + 1] if mi + 1 < len(cuts) else bottom
            # 如果下面紧跟着章节标题，切点必须停在标题上方：否则「二、填空题」
            # 会被裁进上一题的图里，成品中和 layout 画的那一遍重复出现。
            nxt_head = next((hy for hy in stops if hy > mk["y0"] + 1), None)
            if nxt_head is not None and nxt_head - 2 < seg_bot:
                seg_bot = nxt_head - 2
            if seg_bot - seg_top < 3:
                continue
            # 本章节标题到下一章节标题之间的题，归到前一个标题
            sec = sec_at(mk["y0"]) or cur_sec
            if sec:
                cur_sec = sec
            questions.append(
                {
                    "id": mk["label"],
                    "seq": len(questions) + 1,
                    "source_page": pno + 1,
                    "span_pages": 1,
                    "section": sec,
                    "parts": [{"page": pno, "rect": [x0, seg_top, x1, seg_bot], "kind": "body"}],
                }
            )
    return questions


def safe_name(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s)


def sweep(*paths) -> None:
    """删中途产物。**默认什么都不删。**

    为什么不删：执行环境的安全策略会把「删除」换成「移进回收站」，每删一次都弹一个
    确认框等用户点。一轮制作里 profile / reflow / 回归网 / 自测各删一次，用户就得连着
    点三四回，还得判断「这个能不能允许」——而这些目录留着完全不影响结果：

    - `crops/` 按「序号_题号」命名，重跑同名覆盖；题数变了留下的旧文件没人读
      （`layout.py` 只认 `questions.json` 里 `image` 字段指的名字）。
    - 临时 pdf / 临时目录下一次重写。
    - `ocr/` 更是刻意留着的：删了就得重新识别，106 页要 9 分钟。

    真要清理，跑 `tools/clean.py`（一条命令、一次确认），或者临时设 `PDF2WB_SWEEP=1`。
    删不掉也绝不抛出去：沙箱抛的是 `SystemExit(1)` 而不是 `OSError`，漏出去会被上层
    当成「这本书跑挂了」，而那时切点其实已经是对的（2026-11-08 踩过一整天）。
    """
    if os.environ.get("PDF2WB_SWEEP") != "1":
        return
    for p in paths:
        p = Path(p)
        try:
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        except (OSError, SystemExit):
            pass
