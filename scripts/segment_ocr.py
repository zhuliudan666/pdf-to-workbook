"""扫描版 PDF 的切题：用左边栏条带 OCR 找题号，用整页 OCR 找章节。

为什么要分两遍：
整页 OCR 会把「题干以公式开头的题」的题号吞进公式块里，导致整题丢失。
题号永远在最左侧独立成列，单独把左边裁成一条窄带、放大到 300dpi 再识别，
召回率明显高于整页识别（实测能捡回整页漏掉的 2-4% 题目）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import common
import ocr

SEC_RE = re.compile(r"^[一二三四五六七八九]\s*[、.．]\s*(选择题|填空题|解答题|证明题)")
# 章级横幅（不是题、也不分题型，只是排版上的分隔），切题时要整块剔掉
DIVIDER_RE = re.compile(r"命题点|^[※×*]\s*\S")
# 「第一部分 高等数学」「※命题点 4 中值定理及其应用」——做题本的章节层级靠这两行建
PART_RE = re.compile(r"^第\s*([一二三四五六七八九十]+)\s*部分\s*(.*)$")
# 「※命题点」前面那个※在 200dpi 窄带里几乎认不对：实测本书认成过「长」「必」「※」
# 「米」「￥」……原来只放过 [※×*]，于是「长命题点 二次型」「必命题点 概率、条件概率」
# 整章没被识别——书里少了两章，后面章节号全体错位，第三部分第一页还顶着
# 「10. 矩阵的特征值和特征向量」的页眉。现在只要求「命题点」四个字在行首附近，
# 前面允许 0–2 个任意非空白字符（标记 + 可能的误识）。
CHAP_RE = re.compile(r"^[^\s]{0,2}\s*命题点\s*(\d*)\s*(.*)$")
SEC_SHORT = {"选择题": "选", "填空题": "填", "解答题": "解", "证明题": "证"}
# 题号后面的那个方括号，OCR 认成什么都可能：「［」「【」正解，但「『」「〔」也常见——
# 某数三习题册 p92 的「8.「2023数学一］设二」就是左方括号被认成直角引号。方括号不在
# 字符类里，这一行就既不是 QUESTION_RE 也不是 NUM_ONLY_RE，整道题静默消失（成品少一道）。
QUESTION_RE = re.compile(r"^[^\d]{0,3}(\d{1,3})\s*[.．]\s*[\[［【「『〔(（]")
# 「题号这一行」的样子，但号码那一位没读成数字：「l.[2014数学一］设函数」「'.［2015数学二]］」，
# 放大到 600dpi 后那个标点还可能整个丢掉、只剩「.［2015数学二］」。
# 只给 gap_fill 用——它出现的位置必须正好是题号序列的缺口，才敢按形状认。
NUM_GARBLED = re.compile(r"^[^\d]{0,3}[.．]\s*[\[［【「『〔(（]")
NUM_ONLY_RE = re.compile(r"^[^\d]{0,3}(\d{1,3})\s*[.．]\s*$")

INK_DPI = 150


def dark_mask(page, dpi: int = INK_DPI):
    """整页二值掩码（True=墨）。行墨量和书脚识别各要一次，所以一页只渲染一次。"""
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return arr < 150


def row_ink(page, dpi: int = INK_DPI):
    """每一行有多少个深色像素。用来找整行空白带。"""
    return dark_mask(page, dpi).sum(axis=1)


def find_gaps(dark, dpi: int = INK_DPI, min_gap_pt: float = 1.5, ink: int = 2) -> list[tuple[float, float]]:
    """整行空白带（上下界，单位 pt）。门槛要低到 1.5pt——
    题目与题目之间常常只隔着两三个点的白，门槛一高就会漏掉它，
    转而吸附到题目内部选项行之间那条更宽的空白上，把题切两半。"""
    scale = dpi / 72.0
    gaps, start = [], None
    for i, v in enumerate(dark):
        if v <= ink:
            if start is None:
                start = i
        elif start is not None:
            if (i - start) / scale >= min_gap_pt:
                gaps.append((start / scale, i / scale))
            start = None
    if start is not None and (len(dark) - start) / scale >= min_gap_pt:
        gaps.append((start / scale, len(dark) / scale))
    return gaps


def parse_pages(spec, total: int) -> list[int]:
    # 实现搬到 common.py，两条切题路线共用一份（原来这里和 segment 各有一套）
    return common.parse_pages(spec, total)


def strip_lines(pdf: str, pno: int, cfg, out: Path, x0: float | None = None) -> list[dict]:
    """左栏条带 OCR。x0 传了就覆盖 config 里的 strip_x0（加宽重试用，缓存分开存）。"""
    o = cfg["ocr"]
    sx0 = o["strip_x0"] if x0 is None else x0
    # 缓存名里带上 x0：改过 strip_x0 的配置（这本书要把 40 改成 26 才能认出「【例1.1】」）
    # 否则会命中原先那条窄带的旧结果，改了等于没改，还查不出为什么。
    tag = "" if x0 is None else "_w"
    cache = out / "ocr" / f"strip_{pno:03d}{tag}_{int(round(sx0)):03d}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))["lines"]
    doc = pymupdf.open(pdf)
    page = doc[pno - 1]
    clip = (sx0, 0, o["strip_x1"], page.rect.height)
    lines = ocr.ocr_region(page, clip, o["strip_dpi"])
    doc.close()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"page": pno, "clip": clip, "lines": lines}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return lines


def parse_page(pno: int, lines: list[dict], content_top: float, pats: list | None = None,
               x_off: float = 0.0):
    """把一页的左栏行分成三类：题号 / 题型标题 / 章级横幅。

    单独抽出来是因为「加宽重试」要用同一套判据跑第二遍——判据写两处迟早会走偏。

    `pats`：调用方显式配过的题号正则。扫描件的题号不一定长得像「1.［」——
    讲义的例题是「【例1.1】」，跟本题库里还有「选12」「18.3」。内置两条只覆盖
    常见的那几种，剩下的靠配置；命中时把整段题号文字留在 mark["label"] 里，
    后面拿它当题号显示（「例1.1」比「1」有用得多）。

    `x_off`：条带 OCR 的框是**相对裁剪区**的，加上条带左沿才是页面坐标。
    纵向不用加（裁剪从 y=0 起），横向必须加，否则 label_x 永远是「离条带左边多远」，
    拿去做首行归左会整体偏 strip_x0。
    """
    marks, sections, dividers = [], [], []
    for line in lines:
        y = line["box"][1]
        if y < content_top:
            continue
        sec = SEC_RE.match(line["text"])
        if sec:
            sections.append((pno, y, line["box"][3], sec.group(1)))
            continue
        # 「※命题点 2 一元函数微分学」这类章级横幅不是题，也不是章节标题：
        # 它照样会被裁进上一题的图里，印出来是题图中间突然冒出一个大黑块。
        dv = DIVIDER_RE.search(line["text"])
        if dv:
            dividers.append((pno, y, line["box"][3], dv.group(0)))
            continue
        m = QUESTION_RE.match(line["text"]) or NUM_ONLY_RE.match(line["text"])
        if m and int(m.group(1)) != 0:
            # box 两个分支都要给：crop.py 的「首行归左」只认 label_box，
            # 早先只有自定义正则那条分支给了框，走内置正则的书（某数三习题册这种「1.［2010…」）
            # 一律没有 label_box —— 开了 align.first_line_flush 也是静默不生效。
            marks.append({"page": pno, "y": y, "num": int(m.group(1)), "pi": -1,
                          "box": [line["box"][0] + x_off, y,
                                  line["box"][2] + x_off, line["box"][3]],
                          "x": line["box"][0] + x_off, "text": line["text"][:40]})
            continue
        # 题号不可能是 0，但 `A. 0.` 这种选项行在条带里就是「非数字开头 3 字 + 0 + 点 + 结尾」，
        # NUM_ONLY_RE 会把它判成「第 0 题」。52 页真题卷实测多切出 3 道假题
        # （题号序列里冒出「选0」），而且它们在成品上就是一小截选项，看着像题但没题干。
        # 只在为 0 时放过去继续走下面的自定义正则：万一真有一本书从 0 起编号再说。
        for pi, rx in enumerate(pats or []):
            m = rx.match(line["text"])
            if not m:
                continue
            num = 0
            for g in m.groups():
                if g and g.isdigit():
                    num = int(g)
                    break
            # 题号不会是 0。「0.25。其收益情况见表…」这种正文行会被裸数字正则切成
            # 「0.」+ 其余部分，认成一道「第 0 题」——它排在章首时还会把这一章的
            # 编号级数定成 1，连累后面所有真题号（第16章实测）。
            if num == 0:
                continue
            # 题号的显示名取「正则命中的那一段」，不是「行首第一个词」：
            # 按空格切会把「【例1.3】（2014，数一）」整段当成题号（中文里没有空格）。
            # 题号外面那层括号在扫描件里有四种长相：【】、[]、（）以及被认错后混着的，
            # 一律剥掉（「（例1.3）（2014，」就是这么来的：左括号被认成了全角圆括号）。
            label = (m.group(0) or "").strip().strip("【】[]〔〕（）()")
            # 左栏条带里「例1.6」常被认成「例 1.6」（字距被当成空格），
            # 题号里不该有空格，去掉——否则同一本书里「例1.5」和「例 1.6」两种写法并存。
            label = re.sub(r"\s+", "", label)
            # OCR 把「】」吃掉时，后面的年份会粘到子题号上：
            # 「【例1.1】（2021，数二）」→「【例1.12021,数二」。
            # 子题号最多两位，所以末位那段数字一旦到 3 位以上，末四位必是年份，剪掉。
            # 判据只能看「位数」不能看「后面紧跟数字」：粘上来的下一字符是逗号或括号，
            # 不是数字（这条踩过：按后一位是不是数字判，结果一个都没修，全印成例1.12021）。
            label = re.sub(r"\d{3,}$", lambda mm: mm.group(0)[: max(1, len(mm.group(0)) - 4)], label)
            # 「2.4J」「2.5以」：两级题号后面那个字被并进了数字行（条带只有 135pt 宽，
            # 「2.1 用图解法…」常被切成「2. 1」+「用图解法…」两行，第二行偶尔粘一个字母/字过来）。
            # 只剪「数字/点后面紧跟的纯字母」，中文字不动——中文粘上来时更可能是真标签后缀。
            label = re.sub(r"(?<=[\d.])[A-Za-z]+$", "", label)
            # 「16.7.2  转折概率」是节标题，不是第 7 题：题号不会在自己后面再跟一个「.数字」，
            # 那是「章.节.小节」的三级编号。判据必须看**整行剩下的部分**——
            # 正则只吃到「16.7」，label 里根本没有第三段（第一版按 label 判，一次都没命中）。
            # 某运筹学教材第16章实测：它顶替了「16.7」的位置，真 16.7 因为「不大于上一题」被删，
            # 前面 16.1–16.6 更是一路被判成往回走，整章 14 道题只剩 8 道。
            if re.match(r"^\s*[.．]\s*\d", line["text"][m.end():]):
                continue
            # box 留着是给 crop.py 的「首行归左」用的：题号在版心里的真实框。
            # 没有这个框，crop.py 只能靠猜「第一条墨带就是题号那一行」，
            # 而讲义里排在矩阵左侧的题号常常落在第二条、第三条墨带上（例1.2 就是这样，
            # 猜错了会把行列式的第一行单独挪走，整块排版直接散架）。
            marks.append({"page": pno, "y": y, "box": [line["box"][0] + x_off, y,
                                                       line["box"][2] + x_off, line["box"][3]],
                          "x": line["box"][0] + x_off,
                          "num": num, "label": label, "text": line["text"][:40]})
            break
    return marks, sections, dividers


def band_lines(pdf: str, pno: int, cfg, out: Path, y1: float, dpi: int = 200) -> list[dict]:
    """识别一条横贯版心、从版心顶到 y1 的窄带——专门用来读章节标题。

    为什么不复用左栏条带：标题是居中或靠右排的。「※命题点」起于 x≈54，
    左栏（到 175）只看得见这四个字，后面「函数、极限、连续」整个落在栏外；
    「第一部分」更是起于 x≈197，左栏里一个字都没有。靠左栏永远读不出章节名。

    为什么不整页 OCR：整本 106 页重识别要十几分钟，而标题最多二十来处。
    只在「已知有标题」的页上，从版心顶切到标题下方几十点，几十条窄带加起来
    不到一分钟，还不用动已经跑好的题号识别结果。
    """
    o = cfg["ocr"]
    top = max(0, int(o.get("content_top", 40)) - 4)
    y1 = max(top + 30, min(float(y1), 440.0))
    tag = int(round(y1 / 8.0))
    cache = out / "ocr" / f"band_{pno:03d}_{tag:03d}.json"
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))["lines"]
    else:
        doc = pymupdf.open(pdf)
        page = doc[pno - 1]
        clip = (cfg["content_box"][0], top, cfg["content_box"][1], y1)
        raw = ocr.ocr_region(page, clip, dpi)
        doc.close()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps({"page": pno, "clip": clip, "lines": raw}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    # ocr_region 只渲染 clip，返回的坐标是「框内相对坐标」。不把左上角加回去，
    # 每页的标题都会落在 y≈0，章节归属就全乱了。
    ox, oy = cfg["content_box"][0], top
    return [{**ln, "box": [ln["box"][0] + ox, ln["box"][1] + oy,
                           ln["box"][2] + ox, ln["box"][3] + oy]} for ln in raw]


def join_rows(lines: list[dict], tol: float = 9.0, max_gap: float = 110.0) -> list[tuple[list[str], float]]:
    """把同一行上左右拆开的两块字拼回一行，返回 (各段文字, 行顶 y)。

    OCR 按「文字块」切，不按视觉行切：「第一部分」和「高等数学」是两个框
    （x 197–297 / 308–406，纵向只差 3.6pt），「※命题点」和章名也是两个框
    （相隔 72pt）。不拼的话，正则只能看见半句——章节名就一直读不出来。

    返回「分段」而不是整串，是因为同一行右边常常还挂着题目正文
    （「※命题点 中值定理 ※ 设函数…」），拼成一串会把正文吃进标题里。
    只要前两段（标记 + 名字），标题就干净了。
    """
    rows: list[list[dict]] = []
    for ln in sorted(lines, key=lambda r: ((r["box"][1] + r["box"][3]) / 2, r["box"][0])):
        cy = (ln["box"][1] + ln["box"][3]) / 2
        for row in rows:
            ry = (row[0]["box"][1] + row[0]["box"][3]) / 2
            if abs(cy - ry) <= tol:
                row.append(ln)
                break
        else:
            rows.append([ln])
    out_s: list[tuple[list[str], float]] = []
    for row in rows:
        row.sort(key=lambda r: r["box"][0])
        parts: list[str] = []
        last_x = None
        for ln in row:
            if last_x is not None and ln["box"][0] - last_x > max_gap:
                break                      # 隔得太远就不算同一句（右边多半是题目正文）
            parts.append(common.norm(ln["text"]))
            last_x = ln["box"][2]
        if parts:
            out_s.append((parts, min(r["box"][1] for r in row)))
    return out_s


def consensus(items: list[str]) -> str:
    """按字符位投票，拼出「多数页都认对」的那一版。

    页眉只有 8pt，OCR 在不同页上错不同的字：一页把「真」丢掉一横，
    另一页在「数学三」后面多认一个「期」。选「最长的」或「最相似的那个」
    都会把某一页的错字当成对的（实测两种选法都出过错字），
    只有按位投票才能把多数页一致的字挑出来。
    """
    import difflib
    from collections import Counter

    items = [s for s in items if s]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    base = max(items, key=len)
    votes: list[Counter] = [Counter() for _ in base]
    for s in items:
        sm = difflib.SequenceMatcher(None, base, s)
        for op, a0, a1, b0, b1 in sm.get_opcodes():
            if op == "equal":
                for k in range(a1 - a0):
                    votes[a0 + k][base[a0 + k]] += 1
            elif op == "replace":
                for k in range(min(a1 - a0, b1 - b0)):
                    votes[a0 + k][s[b0 + k]] += 1
    out = []
    for i, ch in enumerate(base):
        if not votes[i]:
            out.append(ch)
            continue
        best = max(votes[i].items(), key=lambda kv: (kv[1], kv[0] == ch))
        out.append(best[0])
    return "".join(out)


def detect_book_title(pdf: str, pages: list[int], cfg, out: Path, sample: int = 16) -> str:
    """从原书的页眉里取书名。

    做题本的页眉必须写这本练习册的名字，不能写死一个——写死的结果是
    高数部分印着「线代真题复盘」，一眼就知道是拿别人的成品套的模板。

    取法：原书每页页眉都在同一高度重复印自己的书名（「考研数学 / 真题分类
    互通解：数学三 / 习题册」是三个框拼起来的）。逐页 OCR 页眉那条窄带，
    按相似度归组、按字符位投票。

    为什么必须全扫：书眉是「书名」和「命题点」交替印的，隔六页取一页很可能
    一页书名都碰不上（实测这本 106 页：隔 6 页取 16 页，碰到的全是命题点）。
    窄带 OCR 一条只要 0.12 秒，全扫十几秒，比猜错书名便宜得多。
    页眉字号只有 8pt，单页几乎页页有别字（「考研」认成「老研」、「真题」丢成「题」），
    所以最终结果靠投票，不靠任何单页。

    不直接用 PDF 元数据：扫描件的 title 基本是空的（这本就是空的），
    而文件名往往是 book.pdf 这种顺手起的名字——都比不上书上自己印的。
    """
    if not pages:
        return ""
    # 扫多少页：取页范围开头连续的 30 页，而不是全本。
    # 书眉在「书名」和「命题点」之间逐页交替，连着扫就一定两种都碰到；
    # 每 4 页取 1 页的抽样则可能整步长都踩在某一种上，一本 149 页的书会因为
    # 抽到的全是命题点而读不出书名。全本扫的代价是实测 12 分钟还没结束
    # （这本来是 5 页的活），而书名只要三条完全一致就够定案。
    picks = pages[:30]
    import difflib
    from collections import Counter

    def clean(s: str) -> str:
        s = re.sub(r"[（）()【】\s]+", "", s)          # 投票不看空格和括号
        # 页眉边上常粘着装饰字符（识别出来是「…数学三m」「…数学三 期」这种尾巴）。
        # 书名以汉字结尾，两头的零散字母/数字一律剪掉，但不动中间。
        s = re.sub(r"^[A-Za-z0-9·\-]+", "", s)
        s = re.sub(r"[A-Za-z0-9·\-]+$", "", s)
        return s.strip(" ：:·-")

    cands: list[str] = []
    doc = pymupdf.open(pdf)
    for pno in picks:
        page = doc[pno - 1]
        top = max(4.0, float(cfg["ocr"].get("content_top", 40)) - 4)
        # 页眉字号只有 8-9pt，200dpi 下笔画糊在一起（「考」被认成「老」）。
        lines = ocr.ocr_region(page, (cfg["content_box"][0], 0, cfg["content_box"][1], top), 300)
        best = ""
        for parts, _y in join_rows(lines):
            s = " ".join(parts)
            if sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff") >= 4 and len(s) > len(best):
                best = s
        if best:
            cands.append(best)
        # 早停：书名本来就在书眉里反复印，认出四条一模一样的就已经够投票了。
        # 命题点标题每节都不一样，凑不出四条相同的，所以不会被它提前打断；
        # 万一真凑出来了，也在这里挡掉（后面还有一遍同样的筛）。
        # 门槛给到 4 而不是 3：只有 3 票时按字符位投票的样本太少，
        # 别字盖不干净（实测印出过「考研吡于者师线性代数满分过关“」）。
        if len(cands) >= 8:
            seen = Counter(c for c in (clean(x) for x in cands) if c)
            if seen:
                win, n = seen.most_common(1)[0]
                if n >= 4 and "命题点" not in win and "部分" not in win:
                    break
    doc.close()
    if not cands:
        return ""

    # 书眉在「书名」和「命题点」之间交替。命题点那行前面常被 OCR 粘上几个
    # 装饰字（「三季（命题点3元函数积分学」），所以不能只用行首正则筛，
    # 得按关键字整串筛——否则几页章名会自成一票，把书名挤掉。
    cands = [c for c in cands
             if "命题点" not in c and "部分" not in c and "公众号" not in c
             and not SEC_RE.match(c)]
    norm = [s for s in (clean(c) for c in cands) if s]
    if not norm:
        return ""

    # 先看有没有一版是「完全一致地重复出现」的：这本 106 页里有 8 页把
    # 「考研数学真题分类互通解：数学三」认得分毫不差，直接采用最省事，也最准。
    best, hits = Counter(norm).most_common(1)[0]
    if hits >= max(3, len(norm) // 10):
        return best

    # 没有一条完全一致：把最像的一簇按字符位投票，让多数页的读法把别字盖掉。
    groups: list[list[str]] = []
    for s in norm:
        for g in groups:
            if difflib.SequenceMatcher(None, g[0], s).ratio() >= 0.55:
                g.append(s)
                break
        else:
            groups.append([s])
    top = max(groups, key=len)
    if len(top) >= 3:
        return consensus(top)
    # 票数太少（书里本来就没印几页书名）。宁可空着让调用方去 config 里填，
    # 也不把一处别字印满全书。
    return ""




def title_name(segs: list[str], m) -> str:
    """从「一行分段」里取标题名。

    取第二个文字块，而不是正则的 (.*)：同一行右边常挂着题目正文
    （「※命题点 一元函数微分学 南小最的）求（D）」），(.*) 会把它全吃进标题。
    名字自己也可能被切成两块（「中值定理」+「及其应用」），所以第二块
    如果还是纯中文短词就拼上；带括号、字母、数字的一律当正文丢掉。
    """
    name = segs[1] if len(segs) > 1 else (m.group(2) if m else "")
    # 有时 OCR 把「※」和「命题点 二次型」切成两块，第二块里还带着标记；
    # 再兜一次底，免得标题印成「命题点 二次型」。
    name = re.sub(r"^[^\s]{0,2}\s*命题点\s*\d*\s*", "", name)
    for extra in segs[2:3]:
        if re.fullmatch(r"[\u4e00-\u9fff、·]{2,12}", extra):
            name += extra
    return name.strip()


def footer_top(mask, dpi: int = INK_DPI, ink: int = 2,
               min_blank_pt: float = 8.0, max_band_pt: float = 20.0,
               max_width_ratio: float = 0.5) -> float | None:
    """页底「薄而孤立」的一条墨带上沿——页码候选，不是结论。

    判据从页底往上读：一段空白 → 一块很薄的墨迹 → 又是一段空白，
    而且这块墨不宽（不到版心一半）。页码满足全部条件；正文不满足——
    正文是连续的大块墨，上下不会突然各让出一行白。

    单独看一页会认错：题目最后一行选项也可能又短又孤立。
    所以这只是候选，真伪由 detect_footer() 用「同一高度跨页重复」来定。
    """
    scale = dpi / 72.0
    rowcnt = mask.sum(axis=1)
    h, w = mask.shape
    i = len(rowcnt) - 1
    while i >= 0 and rowcnt[i] <= ink:
        i -= 1
    if i < 0:
        return None
    band_bot = i
    while i >= 0 and rowcnt[i] > ink:
        i -= 1
    band_top = i + 1
    if (band_bot - band_top + 1) / scale > max_band_pt:
        return None
    j = i                              # 墨带上方的空白
    while j >= 0 and rowcnt[j] <= ink:
        j -= 1
    if (i - j) / scale < min_blank_pt:
        return None
    cols = np.flatnonzero(mask[band_top:band_bot + 1].any(axis=0))
    if cols.size == 0:
        return None
    if (int(cols[-1]) - int(cols[0]) + 1) / w > max_width_ratio:
        return None
    return band_top / scale


def detect_footer(doc, pages: list[int], sample_max: int = 12, min_support: float = 0.6,
                  tol_pt: float = 8.0) -> float | None:
    """整本书的书脚页码高度（各页统一），认不出就返回 None。

    页码是最守规矩的东西：几乎每一页都在同一个高度出现一小撮墨。
    所以判据从「这一页看着像页码」升级成「同一高度在多数页上重复出现」——
    偶然出现的短行、公式尾巴凑不出这种队形。

    容差 8pt 而不是精确对齐：书里的页码会随装订偏差上下浮动几个点（实测
    相邻页面能差 6pt），卡死同一个 y 会把它们拆成三票各一，白认。

    取这组里**最小**的那个高度，不取中位数：切点要清掉所有页的页码，
    按中位数切会把偏高的那几页切掉半个数字。

    只抽 sample_max 页：106 页每页都渲染一遍要好几秒，而页码高度不需要全本投票。
    """
    if not pages:
        return None
    sample = pages if len(pages) <= sample_max else pages[:: max(1, len(pages) // sample_max)][:sample_max]
    ys = []
    for pno in sample:
        y = footer_top(dark_mask(doc[pno - 1]))
        if y is not None:
            ys.append(y)
    if len(ys) < 2:
        return None
    best: list[float] = []
    for anchor in ys:
        group = [y for y in ys if abs(y - anchor) <= tol_pt]
        if len(group) > len(best):
            best = group
    if len(best) < max(2, min_support * len(sample)):
        return None
    return min(best)


def ink_bottom(mask, top: float, limit: float, dpi: int = INK_DPI,
               min_ink: int = 5, pad: float = 4.0) -> float:
    """top..limit 之间最后一行「像字」的墨迹下沿（再加 pad）。

    为什么需要：一节最后一题常落在页中间，而它的「底」原本取版心底（或书脚页码
    上沿），于是题图把下半页空白整块吃了进去——裁图看着像半张白纸，排版时还会
    因为「图太高」少放题（实测第 26 页的解24：真身只有 50pt，却被裁成 519pt，
    那一页因此只排得下一道题）。

    为什么门槛是「一行 5 个深色像素」而不是「有墨就算」：扫描脏点只有 1–3 个，
    一颗就够把题图拉回版心底（同一页 y≈704 那颗，让一道两行的解答题带半页白）。
    正常文字行有几十个深色像素，5 与它差两个数量级，不会误伤正文；
    也顺带放过了 0.5pt 的淡横线——那本来就不是内容。
    """
    scale = dpi / 72.0
    r0 = max(0, int(top * scale))
    r1 = min(mask.shape[0], int(limit * scale))
    if r1 <= r0:
        return limit
    rows = np.flatnonzero(mask[r0:r1].sum(axis=1) >= min_ink)
    if rows.size == 0:
        return min(limit, top + pad)
    return min(limit, (r0 + int(rows[-1]) + 1) / scale + pad)


def main(pdf: str, cfg_path: str | None = None, out_dir: str | None = None) -> list[dict]:
    cfg = common.load_config(cfg_path)
    out = Path(out_dir or Path(pdf).parent / "workbook_out")
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf)
    total = len(doc)
    page_h = doc[0].rect.height
    doc.close()

    pages = parse_pages(cfg.get("pages"), total)
    o = cfg["ocr"]
    x0, x1 = cfg["content_box"][0], cfg["content_box"][1]
    content_top = o.get("content_top", 40)
    content_bottom = page_h - o.get("content_bottom_margin", 45)

    # 自定义题号正则：内置三条里只有两条要挡。那两条都是「裸数字 + 点」
    # （`1.（2）` 和 `1. x`），搬到扫描件的左栏条带上会把正文里的「1个，直到…」
    # 「2021 年」全当成题号——条带只有 135pt 宽，正文行首落进来的机会很多。
    #
    # 但 `(1) 设…` 这条**不能**跟着挡：全国卷真题整卷都是 `(1)`–`(23)` 连续编号，
    # 它恰好等于内置默认，于是被当成「用户没改过」过滤掉，整本切出 0 题——
    # 而且不报错（切出 0 题时 structural 也说「无题」）。52 页 17 套真题实测踩到。
    # 括号数字在条带行首出现，几乎只可能是题号：正文里的「式（1）」前面总还有字。
    #
    # `ocr.loose_numbers: true` 是给「裸数字本身就是题号」的书用的：教材课后习题
    # 就是「1. 2. 3.」一条一条，而这条正则恰好被上面那条理由挡住了——不开这个开关
    # 也是 0 题。它安全的前提是 pages 已经圈到「习题」那几页，正文根本不进来；
    # 圈错了页段会切出一堆正文编号，所以它不能默认开。
    STRIP_UNSAFE = {
        r"^\s*(\d+)\s*[.．、]\s*[（(]\s*(\d+)\s*[)）]",
        r"^\s*(\d+)\s*[.．、]\s*(?=\S)",
    }
    loose = bool(o.get("loose_numbers"))
    pats = [re.compile(p) for p in cfg["question_patterns"] if loose or p not in STRIP_UNSAFE]
    # 「答案从这里开始」的行：扫描件也要能停，不然例题的题图里带着整段【详解】
    stop_pats = [re.compile(s) for s in (cfg.get("cut_stop_patterns") or [])]
    stops: dict[int, list[float]] = {}

    marks: list[dict] = []
    sections: list[tuple[int, float, float, str]] = []
    dividers: list[tuple[int, float, float, str]] = []
    widened: list[tuple[int, int]] = []
    for pno in pages:
        lines0 = strip_lines(pdf, pno, cfg, out)
        if stop_pats:
            stops[pno] = sorted(ln["box"][1] for ln in lines0
                                if any(rx.match(ln["text"]) for rx in stop_pats))
        mk, sc, dv = parse_page(pno, lines0, content_top, pats, x_off=o["strip_x0"])

        # 一条题号都没认出来的页，多半不是「这页没有题」，而是题号印到了条带外面。
        # 本书第 15 页就是这样：题号起于 x≈26，而 strip_x0=40 把「19.」整个切掉，
        # 结果整页 19 道题凭空消失，后一页的解答题还因为丢了「三、解答题」被当成填空题。
        # 加宽 16pt 重试一页只要 0.4 秒，比漏一整页便宜太多。
        #
        # 但「一条都没有」这个条件不够：条带切进正文时，正文里的「（2）」「8.」会被当成
        # 题号，于是这页「有题号」了，加宽永远不触发。表现得极隐蔽——成品里这页的
        # 题目全变成「第2题」「第8题」这种野号，然后被「题号必须递增」判据整页删掉，
        # 一页 20 多道题静默消失（某数三习题册全本第 15、16 页，717 题掉到 691）。
        # 所以再加一条：**认出的题号全贴在条带左沿**（离左沿 ≤8pt），说明条带左沿已经
        # 扎进正文本体了，真题号在更左边，照样加宽重试。
        rel_x = [(m.get("x") or 0.0) - o["strip_x0"] for m in mk]
        hugging = bool(mk) and all(rx <= 8 for rx in rel_x)
        if not mk or hugging:
            x0w = max(0.0, o["strip_x0"] - 16)
            mk2, sc2, dv2 = parse_page(pno, strip_lines(pdf, pno, cfg, out, x0=x0w),
                                       content_top, pats, x_off=x0w)
            if len(mk2) >= 2:          # 只捡到一行多半是噪声，宁可漏也不要瞎加
                if hugging:
                    # 加宽后同一行可能读两遍，按 y 去重（同一题号不会差 3pt 以上）
                    keep = [m for m in mk2
                            if all(abs(m["y"] - k["y"]) > 3 for k in mk2 if k is not m)]
                    mk2 = sorted(keep or mk2, key=lambda r: r["y"])
                mk, sc, dv = mk2, sc2, dv2
                widened.append((pno, len(mk2)))

        marks += mk
        sections += sc
        dividers += dv

    for pno, n in widened:
        print(f"  第 {pno} 页：左栏条带认不出题号，向左加宽 16pt 后认回 {n} 题")

    marks.sort(key=lambda r: (r["page"], r["y"]))

    # ---- 题号去伪：解答题的小问被 OCR 认成题号 ----
    # 原文「(Ⅰ) 比较…」在条带里读出来就是「(1)比较…」，于是同一节里题号从 20 掉回 1，
    # 凭空多出几道「小题」——每张图都完整、每道题都像题，只有把题号排成一列才看得出来。
    # 实测 2010 年那套因此多出 1 道。
    #
    # 判据是「同一章同一节内、同一套编号体系，题号必须递增」。四条都不能省：
    # - **在切点之前判**。判完再切，被丢掉的那个题号不会把上一题的底切断；
    #   等切完再删，上一题的图已经被小问那一行截断了（这一版真这么错了一整轮）。
    # - **章要在切点之前就知道**。1..23 每年重排，所以必须按「那一页属于哪一年」分组；
    #   而 stamp_chapters 在流程末尾才把章名写进题目，等不到这里，只能自己按页段算。
    #   不按章分组的后果是：2011 年的第 1 题小于 2010 年的第 23 题，整年全被删掉
    #   （52 页套卷因此只切出 27 题）。
    # - 按**完整编号**比，不能只看第一个数字：「例2.2」的首位是前缀 2，只看它会把
    #   例2.3…例2.15 全判成「不大于上一题」，整章删光（回归网逮住过）。
    # - 编号体系（数字段数）不同就不算同一套：「例2.5」之后冒出个「(1)」，段数是 1，
    #   那是它的小问；真题卷的「(1)」「(20)」段数相同，靠递增判据挡住。
    #
    # 丢弃必须报出来。静默少题最危险——每道题单看都对，只有排成一列才看得出少了谁。
    chap_ranges: list[tuple[int, int, str]] = []
    for ch in cfg.get("chapters") or []:
        try:
            ps = parse_pages(ch.get("pages"), total)
        except Exception:
            continue
        if ps:
            chap_ranges.append((min(ps), max(ps), str(ch.get("title") or "")))

    def chapter_at(pno: int) -> str:
        for a, b, t in chap_ranges:
            if a <= pno <= b:
                return t
        return ""

    def section_at(pno: int, y: float) -> str:
        cur = ""
        for sp, sy, _sy1, name in sections:
            if (sp, sy) <= (pno, y):
                cur = name
            else:
                break
        return cur

    def numkey(mk: dict) -> tuple:
        ds = re.findall(r"\d+", str(mk.get("label") or ""))
        return tuple(int(d) for d in ds) or (mk["num"],)

    # 同名节标题在本节里又出现了一次 → 这是下一章的同一节（「一、选择题」每章一遍），
    # 题号从 1 重排是合法的，不能当成「号码往回走」删掉。ybs-mid 切片就是这种：
    # p3 的「填1..填4」和 p5 的「填1..填4」分属两个命题点，而这片段的章名一个都没认出来
    # （它本来就是「无部分扉页」的切片），只靠 (章, 节) 分组会把后一组整组删光。
    secs_sorted = sorted((sp, sy, name) for sp, sy, _sy1, name in sections)

    def new_sec_between(name: str, p0: int, y0: float, p1: int, y1: float) -> bool:
        return any(name == n and (p0, y0) < (sp, sy) <= (p1, y1) for sp, sy, n in secs_sorted)

    # 换命题点也重排题号。这个「※命题点 3 …」横幅是本书真正的分层：章节名（CHAP_RE）
    # 只在整页横带里读得出来，而左栏条带只扫到「※命」两个字（栏只有 135pt 宽），
    # 章名识别不到——于是同一节里跨了命题点，题号「解1 → 解1」就被当成「往回走」删掉。
    # 某数三习题册全本实测：p89 与 p101 各有一处，删掉 3 道真题号，成品直接从 717 掉到 714。
    # 判据只认带「命」的横幅（「※命」「※命题点」都算），别的 ※ 注记不算换组。
    div_sorted = sorted((dp, dy) for dp, dy, _dy1, nm in dividers if "命" in nm)

    def new_chap_between(p0: int, y0: float, p1: int, y1: float) -> bool:
        return any((p0, y0) < (dp, dy) <= (p1, y1) for dp, dy in div_sorted)

    # ---- 章号锚定：教材课后习题的题号就写着章号 ----
    # 「第9章 动态规划」的习题一定叫 9.1、9.2……章号在标题里就有了，不用再猜「这一章
    # 用几级编号」。判据一换，正文里那些「（1）（2）（3）」自然全被挡在外面。
    # 某运筹学教材第9章实测：正文解释段的三行「（1）…（2）…（3）…」被条带读成题号，
    # 该章第一个题号就成了「3」，于是 9.1–9.6 全被判成「级数不同」删光，
    # 整章只切出 2 道假题（题号「3」「5」，题干是正文的「能利用经验，提高…」）。
    # 只在「本章真有一批 章.序 题号」时才启用，不影响「1. 2. 3.」和「(1) (2)」那两类书。
    CH_NUM_RE = re.compile(r"第\s*(\d+)\s*章")
    chap_num: dict[int, int] = {}
    for _a, _b, _t in chap_ranges:
        _m = CH_NUM_RE.search(_t)
        if _m:
            for _p in range(_a, _b + 1):
                chap_num[_p] = int(_m.group(1))

    def grp_of(mk: dict) -> tuple:
        return (chapter_at(mk["page"]), section_at(mk["page"], mk["y"]))

    anchored: dict[tuple, int] = {}
    for mk in marks:
        cn = chap_num.get(mk["page"])
        if cn is None:
            continue
        k = numkey(mk)
        if len(k) == 2 and k[0] == cn:
            anchored[grp_of(mk)] = cn

    # 同组内「下一题」的题号，给「数字粘连」纠正用（见下）
    grp_seq: dict[tuple, list[int]] = {}
    for i, mk in enumerate(marks):
        grp_seq.setdefault(grp_of(mk), []).append(i)

    def next_key_of(gi: int) -> tuple | None:
        g = grp_of(marks[gi])
        lst = grp_seq[g]
        pos = lst.index(gi)
        return numkey(marks[lst[pos + 1]]) if pos + 1 < len(lst) else None

    # 锚定住了就直接定级：这一章用两级编号。否则「本章第一个题号」还是可能是
    # 正文里的一行「0.25」——它一旦排在 16.1 前面，main_pi 就成了 1，
    # 16.1–16.6 明明过了章号锚定，又被级数判据删一遍（第16章实测：锚定白做）。
    kept, dropped = [], []
    last_key: dict = {}
    main_pi: dict = {g: 2 for g in anchored}
    first_key: dict = {}
    fixed_nums: list[tuple] = []
    # 括号小问：「(1) 求 Y 的分布函数…」这种行同样以括号数字开头，但它是小问——
    # 判据不是「有没有括号」（真题卷整卷题号就是括号），而是**缩进**：题号顶在题号栏，
    # 小问比题号栏至少缩进 6pt。某数三习题册 p89 实测：题号 x≈68.9，小问 x≈81.6。
    # 早先按「组内多数长相」判过，一组里小问比题号多（每题 3 个小问）就整组判反。
    PAREN_SUB = re.compile(r"^[（(]\s*\d{1,2}\s*[)）]")

    # 离题号栏太远的「题号」不是题号。题号都顶在版心左沿，正文里的小问、
    # 公式编号、跨行留下的「10.」「X≥2.」会散在版心深处——某数三习题册 p92 正文里就有个
    # 「10.」落在 x≈159（题号栏在 x≈59），它一进序列，后面真 7、8 全被判「往回走」
    # 删掉，一本 717 题的书最后少 3 道、还多出两道野号。
    # 阈值取「本组题号 x 的中位数 + 45pt」：奇偶页版心差最多十几 pt，够宽。
    gx: dict[tuple, list[float]] = {}
    for mk in marks:
        gx.setdefault(grp_of(mk), []).append(mk.get("x") or 0.0)
    col_x = {g: sorted(v)[len(v) // 2] for g, v in gx.items()}

    # 题号栏左沿：本组里「裸数字 + 点」的行（走的是内置正则，label 是空的）就是真题号，
    # 它们的 x 最小值就是这一栏的左边。为什么不拿全组 x 的中位数当栏位：某数三习题册全本的
    # 解答题组里每道题带 2–3 个小问，括号行比题号还多，中位数 74.4 直接落在**小问那一列**，
    # 于是 x=77 的「（2）」只比中位数大 2.6pt，判不出缩进；它一进序列，「2」就把真解2
    # 顶掉（真解2 反倒被「题号往回走」删掉），一本 717 题的书少 5 道、多出两道野号。
    # 裸数字至少 3 个才认这一栏：真题卷整卷都是「(1)」编号，那时没有裸数字，
    # 一两个噪声行（「X≥2.」）不足以代表题号栏，退回中位数判据。
    bare_x: dict[tuple, list[float]] = {}
    for mk in marks:
        if not mk.get("label"):
            bare_x.setdefault(grp_of(mk), []).append(mk.get("x") or 0.0)
    col_left = {g: min(v) for g, v in bare_x.items() if len(v) >= 3}

    def _too_far_right(mk: dict) -> bool:
        return (mk.get("x") or 0.0) > col_x.get(grp_of(mk), 1e9) + 45

    def _is_sub_item(mk: dict) -> bool:
        if not PAREN_SUB.match(str(mk.get("text") or "").lstrip()):
            return False
        g = grp_of(mk)
        left = col_left.get(g)
        if left is None:
            return (mk.get("x") or 0.0) > col_x.get(g, 0.0) + 6
        return (mk.get("x") or 0.0) > left + 6

    for gi, mk in enumerate(marks):
        grp = grp_of(mk)
        key = numkey(mk)
        cn = anchored.get(grp)
        if cn is not None and (len(key) != 2 or key[0] != cn):
            dropped.append((mk, "（不是本章题号）"))
            continue
        if _too_far_right(mk):
            dropped.append((mk, "（离题号栏太远，是正文里的编号）"))
            continue
        if _is_sub_item(mk):
            dropped.append((mk, "（括号小问，缩进在题号栏右边）"))
            continue
        # 「这一章用几级编号」由该章第一个题号定，级数不同的就不是题号。
        # 比「按正则编号判」稳：同一套写法常有多个变体（`(11)`、`（11）`、`(21（`、
        # 甚至只认出 `11）`），每个变体都是一条正则；而**级数**是稳定的——
        # 卷子是「(1)」一级，教材课后习题是「2.1」两级、小问才是「(1)」一级。
        # 不按级数分的后果：2026 那套整年只剩 8 题（2010–2025 是括号、2026 换成「1.」）。
        if grp not in main_pi:
            main_pi[grp] = len(key)
        prev_key, prev_id, prev_p, prev_y = last_key.get(grp, (None, None, 0, 0.0))
        # 数字粘连纠正：「10.43」其实是「10.4」，题号后面多粘了一位（同「2.4J」那种）。
        # 判据看紧邻的下一题：若下一题正好是「上一题+2」，那这一题本该是「上一题+1」，
        # 多出来的那位是脏字。某运筹学教材第10章实测：10.43 一成立，后面的 10.5…10.18
        # 每一个都「不大于上一题」被删，整章 18 道只剩 4 道。
        # 只在这个组合下动手，真跳号（3 缺 4 直接到 5，下一题是 6）不会被误改。
        if (prev_key is not None and len(key) == len(prev_key) == 2
                and key[0] == prev_key[0] and key[1] > prev_key[1] + 1
                and re.fullmatch(r"[\d.．]+", str(mk.get("label") or ""))):
            nk = next_key_of(gi)
            if nk is not None and len(nk) == 2 and nk[0] == key[0] and nk[1] == prev_key[1] + 2:
                new_key = (key[0], prev_key[1] + 1)
                fixed_nums.append((mk.get("label"), ".".join(map(str, new_key)), mk["page"]))
                mk = dict(mk, label=".".join(map(str, new_key)))
                key = new_key
        if len(key) != main_pi[grp]:
            dropped.append((mk, "（编号级数不同）"))
            continue
        # 两级编号的前缀就是章号，一章之内不该变。「2.10」下面表格里的「3. 40」
        # 被条带读到，一旦让它过，后面的「2. 11」就小于 3.40 被当成往回走删掉
        # （第2章习题实测：2.1–2.10 全对，2.11 丢，末尾多出一道「3.40」）。
        if len(key) > 1 and first_key.get(grp) is not None and key[:-1] != first_key[grp][:-1]:
            dropped.append((mk, "（前缀不是本章章号）"))
            continue
        if prev_key is not None and key <= prev_key:
            # 中间又出现了同名节标题 → 换节重排，放行；中间换了命题点也一样
            if not (new_sec_between(grp[1], prev_p, prev_y, mk["page"], mk["y"])
                    or new_chap_between(prev_p, prev_y, mk["page"], mk["y"])):
                dropped.append((mk, "（题号往回走）"))
                continue
        kept.append(mk)
        first_key.setdefault(grp, key)
        last_key[grp] = (key, mk.get("label") or str(mk["num"]), mk["page"], mk["y"])
    if fixed_nums:
        print("  题号纠正：" + "、".join(f"p{p}「{a}」→「{b}」" for a, b, p in fixed_nums[:6])
              + (f" 等 {len(fixed_nums)} 处" if len(fixed_nums) > 6 else "")
              + "（题号后面粘了脏字，按上下题号序列更正）")
    # 单页级复盘：题号序列看着不对时，别靠猜是哪条判据动的手——
    # `SEG_DEBUG_PAGES=88,89` 跑一遍，留下的和丢掉的逐行列出来（含 x，缩进类判据全靠它）。
    _dbg = {int(v) for v in re.split(r"[,，\s]+", os.environ.get("SEG_DEBUG_PAGES") or "") if v.strip().isdigit()}
    if _dbg:
        for _m, _w in sorted(dropped, key=lambda t: (t[0]["page"], t[0]["y"])):
            if _m["page"] in _dbg:
                print(f"    [丢] p{_m['page']} y={_m['y']:6.1f} x={(_m.get('x') or 0):6.1f} "
                      f"n={_m['num']} lbl={_m.get('label')!r} {_w} txt={str(_m.get('text'))[:26]!r}")
        for _m in kept:
            if _m["page"] in _dbg:
                print(f"    [留] p{_m['page']} y={_m['y']:6.1f} x={(_m.get('x') or 0):6.1f} "
                      f"n={_m['num']} lbl={_m.get('label')!r} txt={str(_m.get('text'))[:26]!r}")
        _gs = {grp_of(_m) for _m in marks if _m["page"] in _dbg}
        for _g in sorted(_gs):
            if _g in col_x:
                _v = sorted(gx[_g])
                print(f"    组 {_g} 题号栏 x 中位={col_x[_g]:.1f} 左沿={col_left.get(_g)} "
                      f"最小={_v[0]:.1f} 最大={_v[-1]:.1f} n={len(_v)}")
        for _sp, _sy, _sy1, _nm in sections:
            if _sp in _dbg:
                print(f"    [节] p{_sp} y={_sy:6.1f} {_nm}")
        for _dp, _dy, _dy1, _nm in dividers:
            if _dp in _dbg:
                print(f"    [横幅] p{_dp} y={_dy:6.1f} {_nm}")

    if dropped:
        shown = "、".join(f"p{m['page']}「{m.get('label') or m['num']}」" for m, _ in dropped[:5])
        more = f" 等 {len(dropped)} 处" if len(dropped) > 5 else ""
        # 按原因分开数。只说「丢弃 231 处」没法判断是真在清小问，还是把真题号误删了——
        # 后者每本都不同、而且不报错，只有分原因计数才看得出来。
        tally: dict[str, int] = {}
        for _m, why in dropped:
            tally[why] = tally.get(why, 0) + 1
        why = "、".join(f"{k}×{v}" for k, v in sorted(tally.items(), key=lambda kv: -kv[1]))
        print(f"  丢弃 {len(dropped)} 个不像题号的行：{shown}{more}（{why}）")
    marks = kept

    # ---- 补漏：整行题号被 OCR 丢掉时，在上下两题的夹缝里重识别 ----
    # 一条 135pt 宽、整页高的窄带一次识别，遇到「题号紧贴上面那道大分式」的行会整行消失
    # （2013 数三第 11 题就是这样：`(11)` 和上面的积分挤在一起，整行没出来，
    # 题号序列于是从 10 直接跳到 12，成品少一道题而不报错）。
    # 把夹缝单独裁出来放大到 400dpi 再识别，同一行立刻读得出来——所以按「题号序列缺口」补，
    # 而不是整本重跑：51 页重跑 5 分钟，补漏只碰十几处、几秒钟。
    def gap_fill(base: list[dict]) -> list[dict]:
        groups: dict = {}
        for mk in base:
            groups.setdefault((chapter_at(mk["page"]), section_at(mk["page"], mk["y"])), []).append(mk)
        added: list[dict] = []
        x_lo = max(0.0, o["strip_x0"] - 15)
        doc2 = pymupdf.open(pdf)
        # 补到的题号必须和上下两道**写法一样**：「填18」之间只能补「填19」，
        # 不能补一个正文里的「（2）」或另一套编号的「8」。跨页扫描一次就是大半页，
        # 判据只剩「级数对、号码在缺口里」的话，正文小问会成群混进来——
        # 某数三习题册全本实测：多出十几个「2」「3」，整本 717 题被打乱成 691 题。
        # 写法（把数字都换成 #）比号码可靠得多：3.6/3.7 都是「#.#」，填1/填2 都是「填#」。
        def textkey(mk: dict) -> str:
            return re.sub(r"\d+", "#", str(mk.get("label") or "")) or "#"
        try:
            for lst in groups.values():
                lst.sort(key=lambda r: (r["page"], r["y"]))
                for a, b in zip(lst, lst[1:]):
                    ka, kb = numkey(a), numkey(b)
                    if len(ka) != len(kb) or kb[-1] - ka[-1] <= 1:
                        continue
                    ta, tb = textkey(a), textkey(b)
                    if ta != tb:
                        continue
                    # 缺口可以跨页。「3.6」在 p103 下半页、「3.8」在 p104 页顶，中间那道
                    # 「3.7」被 表3-28 挤成一行没认出来——只查同页的夹缝就永远补不到，
                    # 成品静默少一道（某运筹学教材第3章实测：3.6→3.8，缺 3.7）。
                    spans = []
                    if a["page"] == b["page"]:
                        spans.append((a["page"], a["y"] + 1, b["y"] - 1))
                    elif b["page"] == a["page"] + 1:
                        spans.append((a["page"], a["y"] + 1, 9e9))
                        spans.append((b["page"], 0.0, b["y"] - 1))
                    else:
                        continue
                    spans = [s for s in spans if s[2] - s[1] >= 8]
                    if not spans:
                        continue
                    want = set(range(ka[-1] + 1, kb[-1]))
                    if _dbg and (a["page"] in _dbg or b["page"] in _dbg):
                        print(f"    [补漏试] {a.get('label') or a['num']}(p{a['page']} y{a['y']:.0f})"
                              f" → {b.get('label') or b['num']}(p{b['page']} y{b['y']:.0f})"
                              f" 缺 {sorted(want)} 区段 {[(p, round(y0), round(y1)) for p, y0, y1 in spans]}")
                    # 400dpi 一般够；仍读不出就上 600dpi——夹缝只有几 pt 高，放大不贵。
                    # （2022 数三第 2 题：题号紧贴上面那道数列通项，400dpi 整行还是不出来，
                    # 600dpi 一次就读到了。）
                    for dpi_try in (400, 600):
                        if not want:
                            break
                        for pno2, y0, y1 in spans:
                            if not want:
                                break
                            if y1 > 9e8:
                                y1 = doc2[pno2 - 1].rect.height
                            lines = ocr.ocr_region(doc2[pno2 - 1], [x_lo, y0, 245, y1], dpi=dpi_try)
                            # ocr_region 的坐标是相对裁剪区的，要加回裁剪原点
                            for ln in lines:
                                b0, b1, b2, b3 = ln["box"]
                                ln["box"] = [b0 + x_lo, b1 + y0, b2 + x_lo, b3 + y0]
                            mk2, _s, _d = parse_page(pno2, lines, content_top, pats, x_off=0.0)
                            for m in mk2:
                                n = numkey(m)[-1]
                                if (n in want and len(numkey(m)) == len(ka)
                                        and textkey(m) == ta):
                                    added.append(m)
                                    want.discard(n)
                            # 还有一种漏法：行在、形状也对，**只有号码那一位被认成标点或字母**
                            # （「4.」→「l.」、「7.」→「'.」）。这种行 parse_page 一律读不出来，
                            # 600dpi 也一样——它认的不是「缺题号」，而是「这行没有题号」。
                            # 缺口里只剩一个待补号码时，按形状认它：某数三习题册 p15 的
                            # 解4「l.[2014数学一］」和解7「'.［2015数学二]］」就是这样丢的，
                            # 成品少两道、上一题的图还把它们的题干吞进去。
                            if len(want) == 1:
                                tgt = next(iter(want))
                                for ln in lines:
                                    if ln["box"][0] > x_lo + 60:      # 不在题号栏里
                                        continue
                                    if NUM_GARBLED.match(ln["text"]):
                                        added.append({"page": pno2, "y": ln["box"][1],
                                                      "box": list(ln["box"]),
                                                      "x": ln["box"][0], "num": tgt,
                                                      "label": None,
                                                      "text": ln["text"][:40]})
                                        want.discard(tgt)
                                        break
        finally:
            doc2.close()
        return added

    filled = gap_fill(marks)
    if filled:
        print("  补漏：夹缝重识别找回 " + "、".join(
            f"p{m['page']}「{m.get('label') or m['num']}」" for m in filled[:8])
            + (f" 等 {len(filled)} 处" if len(filled) > 8 else ""))
        marks = sorted(marks + filled, key=lambda r: (r["page"], r["y"]))

    # ---- 章节层级：部分 / 命题点 ----
    # 左栏条带只看得见「※命题点」四个字，章名在栏外（x≈186 起），所以对每个
    # 有标题的页，再补一条横贯版心的窄带 OCR，把「第一部分 高等数学」
    # 「※命题点 一元函数微分学」整句读出来。
    anchor: dict[int, float] = {}
    for pno, y, _y1, _t in dividers:
        anchor[pno] = max(anchor.get(pno, 0.0), y)
    for pno, y, _y1, _t in sections:
        anchor[pno] = max(anchor.get(pno, 0.0), y)
    catalog: list[tuple[int, float, str, str]] = []   # (页, y, part|chap, 标题)
    part_no = chap_no = 0
    for pno in sorted(anchor):
        for segs, hy in join_rows(band_lines(pdf, pno, cfg, out, anchor[pno] + 40)):
            # 页眉那条「命题点1 函数、极限、连续」是原书的书眉，不是新的章开始。
            # 它紧贴版心顶，切在 content_top 以下就能挡住；挡不住的话，
            # 每翻一页都会认出一个「新章」，章节页会多出十几张。
            if hy < content_top - 2:
                continue
            s = " ".join(segs)
            m = PART_RE.match(s)
            if m:
                part_no += 1
                catalog.append((pno, hy, "part", f"第{m.group(1)}部分 {title_name(segs, m)}".strip()))
                continue
            m = CHAP_RE.match(s)
            if m:
                name = title_name(segs, m)
                if name:
                    chap_no += 1
                    catalog.append((pno, hy, "chap", f"{chap_no}. {name}".strip()))
    catalog.sort(key=lambda h: (h[0], h[1]))

    def last_head(kind: str, pno: int, y: float) -> str:
        found = ""
        for hp, hy, hkind, label in catalog:
            if (hp, hy) > (pno, y):
                break
            if hkind == kind:
                found = label
        return found


    gap_cache: dict[int, list[tuple[float, float]]] = {}
    mask_cache: dict[int, object] = {}
    doc = pymupdf.open(pdf)

    def mask_of(pno: int):
        if pno not in mask_cache:
            mask_cache[pno] = dark_mask(doc[pno - 1])
        return mask_cache[pno]

    def gaps_for(pno: int):
        if pno not in gap_cache:
            gap_cache[pno] = find_gaps(mask_of(pno).sum(axis=1))
        return gap_cache[pno]

    # 书脚页码不属于任何一题。不切掉，最后一题的题图会一直拉到版心底、
    # 把「· 2 ·」焊在图里，做题本上那道题看着顶到纸边、答题区没处放。
    foot_y = detect_footer(doc, pages)

    def bottom_of(pno: int) -> float:
        if foot_y is None:
            return content_bottom
        # 页码位置离谱（高到版心中间）时不信它，仍按版心算
        return min(content_bottom, max(foot_y - 2, content_top + 40))

    def last_ink_y(pno: int, top: float, limit: float) -> float:
        return ink_bottom(mask_of(pno), top, limit)

    # 墨带（连续的「有墨的行」）缓存：切点落进墨带内部，等于把一行字/一个矩阵的
    # 括号竖线切在半腰。矩阵、行列式那种竖线比末行文字还低 3-4pt，切在末行基线上
    # 看着「刚好」，印出来括号是缺的。
    band_cache: dict[int, list[tuple[float, float]]] = {}

    def ink_bands(pno: int, ink: int = 1) -> list[tuple[float, float]]:
        if pno not in band_cache:
            rows = mask_of(pno).sum(axis=1)
            scale = INK_DPI / 72.0
            bands, start = [], None
            for i, v in enumerate(rows):
                if v > ink:
                    if start is None:
                        start = i
                elif start is not None:
                    bands.append((start / scale, i / scale))
                    start = None
            if start is not None:
                bands.append((start / scale, len(rows) / scale))
            band_cache[pno] = bands
        return band_cache[pno]

    def nudge_out(pno: int, y: float, side: str, cap: float = 6.0) -> float:
        """y 落在墨带里时，往这条带的**外侧**挪出去，且最多挪 cap 个点（side=顶/底）。

        只挪一点点是故意的：括号竖线、行的下伸笔画正好探出 3-4pt，挪 4pt 就够；
        而如果切点深深地落在一行字里（差 30pt 那种），挪出去只会把上一题整行
        拖进来——那种错属于切点逻辑，不该在这里掩盖。
        方向固定朝外（顶边界往上、底边界往下）：朝里挪虽然也是「干净的切」，
        但那一整行就从两题之间消失了，比多带一丝墨更糟。
        """
        for a, b in ink_bands(pno):
            if a + 0.6 < y < b - 0.6:
                if side == "top":
                    return max(content_top, a - 1.0) if y - a <= cap else y
                return min(content_bottom, b + 1.0) if b - y <= cap else y
        return y

    # 题与题之间的空白带一般 ≥5pt；题内公式与题号行之间的缝常见 2-3pt。
    # 只用宽度过滤会把「一页十七题、行距极紧」的填空题筛到无候选，所以门槛取 4pt。
    MIN_GAP_PT = 4.0

    def snap_above(pno: int, y: float) -> float:
        """把边界吸附到 y 上方那条「题与题之间」的空白带的中心。

        为什么不能直接用 OCR 文字行的 y 当边界：题号所在的文字行并不等于题目
        的上边缘，紧跟在题号后面的大分式、积分号、矩阵、分段函数的大括号会把
        内容向上顶出十几到二十几 pt。直接按文字行切，就会把下一题的公式削掉一
        条，留在上一题底部变成「残渣」。

        窗口取 [y-30, y+4]：上界留一点余量，是因为 OCR 的文字框顶常常和上方空
        白带的结束位置只差半个点（41 题那行只差 0.7pt），卡死在 y-6 会把唯一正
        确的那条带排掉，于是退到上一题内部去切。下界 30pt 是因为本题自己的行
        都在 y 以下，窗口往上扫不到它们。
        """
        above = [
            g for g in gaps_for(pno)
            if y - 30 <= g[1] <= y + 4 and (g[1] - g[0]) >= MIN_GAP_PT
        ]
        if not above:
            return max(content_top, y - 12)
        a, b = max(above, key=lambda t: t[1])      # 取最靠下的那条，离本题最近
        return max(content_top, min((a + b) / 2, y - 1))

    def block_top(pno: int, y: float, top: float, tall: float = 18.0) -> float:
        """题号落在一条「高个子墨带」里时，把上边界退到这条带的顶上。

        为什么需要：多行矩阵、行列式、大括号分段函数的竖线是连着的，整块只有
        一个墨带；题号往往与这块的**中间某一行**齐平（书里是为了版面好看），
        吸附出来的边界于是切在墨带内部，成品里矩阵少掉上面几行——不是切歪半行，
        是整行消失，学生按缺条件的题做。实测例1.8 的六行行列式只剩五行、
        例2.5 的三行只剩两行。
        判据用「带高 ≥18pt」（一行正文连上下伸笔画约 13pt）：普通行不受影响，
        所以不会把上一题的行拖进来；高个子块则整块保住。
        """
        for a, b in ink_bands(pno):
            if a <= y <= b:
                if b - a >= tall:
                    return max(content_top, min(top, a - 2.0))
                break
        return top

    # 给每道题分配所属章节；章节切换时题号重新计数
    def section_of(pno: int, y: float) -> str:
        cur = ""
        for spno, sy, _sy1, name in sections:
            if (spno, sy) <= (pno, y):
                cur = name
            else:
                break
        return cur

    # 逐页算边界：先吸附到空白带，再用相邻两题的文字行把结果夹住，保证不退化
    per_page: dict[int, list[dict]] = {}
    for mk in marks:
        per_page.setdefault(mk["page"], []).append(mk)

    head_limit: dict[int, float] = {}
    questions = []
    for pno in pages:
        page_marks = sorted(per_page.get(pno, []), key=lambda r: r["y"])
        if not page_marks:
            continue
        bounds = []
        prev_y = content_top - 1
        for mk in page_marks:
            snap = snap_above(pno, mk["y"])
            lo, hi = prev_y + 5, mk["y"] - 1
            # 填空题一页十七道，行间几乎没空白，吸附会把两题吸到同一个位置，
            # 这时退回两行中点，宁可切在题与题的中间也不能让某一题高度归零
            bounds.append((prev_y + mk["y"]) / 2 if hi <= lo else min(max(snap, lo), hi))
            prev_y = mk["y"]

        # 兜底：一题连 24pt 都不到，说明吸附点落到了本题内部，退回「题号上方 12pt」
        for i in range(1, len(page_marks)):
            if bounds[i] - bounds[i - 1] < 24:
                bounds[i] = max(bounds[i - 1] + 12, page_marks[i]["y"] - 12)
        bounds.append(bottom_of(pno))

        # 本页所有「不属于任何一题」的横条：章节标题 + 章级横幅，切题时都要从题图里剔掉。
        # 横幅往外多留 12pt：OCR 的行框只框住文字，大黑块/六边形比字高出半行，
        # 按文字上沿切会把块顶留在图里。
        mids = [(sy, sy1) for spno, sy, sy1, _ in sections if spno == pno]
        mids += [(max(content_top, dy - 12), dy1 + 2) for dpno, dy, dy1, _ in dividers if dpno == pno]

        # 页首的横条（「二、填空题」、有时还带「※命题点 4」）不能被算进上一题的续页。
        # 只看章节标题不够：续页是从 content_top 起切的，横幅正好卡在这条线上，
        # 结果一道题的下半页接了一整块「命题点 4 中值定理及其应用」。
        above = [sy for sy, _sy1 in mids if sy < page_marks[0]["y"]]
        limit = (min(above) - 4) if above else bounds[0]
        head_limit[pno] = max(content_top, min(limit, bounds[0]))

        # 本页第一题：章节标题行要切掉，否则成品里会出现两遍「二、填空题」。
        # 标题行下沿之后通常没有空白带可吸附（题号紧跟着就是正文），只能硬切。
        heads = [(sy, sy1) for spno, sy, sy1, _ in sections if spno == pno and sy < page_marks[0]["y"]]
        if heads:
            bounds[0] = max(bounds[0], min(heads[-1][1] + 3, page_marks[0]["y"] - 6))

        for i, mk in enumerate(page_marks):
            sec = section_of(pno, mk["y"])
            # 没有「一、选择题」这类标题的书（讲义、例题册），以前在这里整页被丢掉：
            # section 为空就 continue，跑完的结果是「一题都没切出来」。既然题号是
            # 配置显式给的，题型可以没有，题不能没有。
            if not sec and not mk.get("label"):
                continue
            top, bottom = bounds[i], bounds[i + 1]
            # 题号与一个「高个子墨块」（多行矩阵、行列式）的中间行齐平时，
            # 吸附出来的边界会落在块内部，把上面几行切掉——退到块顶。
            top = block_top(pno, mk["y"], top)
            # 退上去以后会和上一题的「底」重叠：上一题的图里也带着这几行，
            # 同一块矩阵在两题上各印一遍。所以同步把上一题的底收回来。
            if questions and questions[-1]["parts"][-1]["page"] == pno - 1:
                prev = questions[-1]["parts"][-1]["rect"]
                if prev[3] > top:
                    prev[3] = max(prev[1] + 4, top)
            # 页中也可能夹着章节标题：题 23 和解答题 1 之间就有一行「三、解答题」。
            # 标题不属于任何一题，落在谁的图里就得切掉——否则成品里它会印两遍：
            # 一遍是题图里的原书字，一遍是排版按 section 新画的。
            for sy, sy1 in mids:
                if sy >= bottom or sy1 <= top:
                    continue
                if sy - top >= bottom - sy1:      # 标题下面内容更多：它归上一题，往下切
                    bottom = max(top + 4, sy - 4)
                else:                             # 否则归下一题，往上切
                    top = min(bottom - 4, sy1 + 2)
            # 解答开始的那一行（【详解】/解：/证明：）之后全是答案，不能进题图。
            # 只在下一题之前找，跨过下一题去找会把本题的底压到别人身上。
            s = next((sy for sy in stops.get(pno, []) if sy > top + 2), None)
            if s is not None and s - 4 < bottom:
                bottom = max(top + 4, s - 4)
            # 页尾那题的「底」原本取到版心底，会把下半页空白全裁进题图
            bottom = max(top + 4, last_ink_y(pno, top, bottom))
            # 最后一道保险：切点落在墨带内部就往外挪 1pt——行列式/矩阵的括号竖线
            # 比末行文字低 3-4pt，切在末行上看似刚好，印出来括号是断的（qa 报「切穿」）。
            top = nudge_out(pno, top, "top")
            bottom = nudge_out(pno, bottom, "bottom")
            if bottom - top < 4:
                continue
            questions.append(
                {
                    # 用了自定义正则就以原书写法为题号（「例1.1」），比只剩一个
                    # 数字有用：成品上标「例1.1」，翻原书能对上。
                    "id": (mk.get("label") or "").strip("【】[]") or
                          f"{SEC_SHORT.get(sec, sec)}{mk['num']}",
                    "num": mk["num"],
                    "section": sec,
                    "part": last_head("part", pno, mk["y"]),
                    "chapter": last_head("chap", pno, mk["y"]),
                    "source_page": pno,
                    # 题号在版面里的框（pt）。crop.py 的 align.first_line_flush
                    # 靠它认出「哪一条墨带才是题号那一行」。
                    "label_box": mk.get("box"),
                    "span_pages": 1,
                    "parts": [{"page": pno - 1, "rect": [x0, top, x1, bottom], "kind": "body"}],
                }
            )
    # 跨页：上一题占满页底时，把下一页页首（第一个题号以上）的内容接上去。
    # doc 先别关：合并要判断「上一题的底有没有压到本页底边」，而底边可能被
    # 书脚页码抬高，得现算。
    if cfg["cross_page"]["merge_continuation"]:
        by_page_first = {}
        for q in questions:
            p = q["parts"][0]["page"]
            by_page_first.setdefault(p, q)
        merged = []
        for q in questions:
            p = q["parts"][0]["page"]
            prev_page = (merged[-1]["parts"][-1]["page"] + 1) if merged else None
            # p == prev_page：续页必须紧挨着上一页。页段可以是离散的
            # （"3,6,9,12"），那时 p 与上一题的页之间隔着一整章正文，
            # 接上去就是把别人的正文切进本题。
            if (
                q is by_page_first.get(p)
                and merged
                and p == prev_page
                and merged[-1]["parts"][-1]["rect"][3] >= bottom_of(prev_page) - 6
            ):
                prev = merged[-1]
                head_bottom = head_limit.get(q["source_page"], q["parts"][0]["rect"][1])
                if head_bottom - content_top > 5:
                    prev["parts"].append(
                        {"page": p, "rect": [x0, content_top, x1, head_bottom], "kind": "cont"}
                    )
                    prev["span_pages"] += 1
            merged.append(q)
        questions = merged

    for i, q in enumerate(questions, 1):
        q["seq"] = i
    questions.sort(key=lambda q: q["seq"])
    questions = [{**q, "seq": i} for i, q in enumerate(questions, 1)]
    for q in questions:
        q.pop("num", None)
    doc.close()

    # 章：扫描件里只有「第X部分 / 命题点」这种横幅会被自动认出来，
    # 而讲义常按「第一章 行列式」分章——横幅印在章首那页的中部，左栏条带里
    # 常常只剩「第五章」三个字，标题整个落在栏外，认出来也不成句。
    # 所以给一条和文本层路线一样的路：配置里写 chapters（章名 + 页段），按页盖。
    stamped = common.stamp_chapters(questions, cfg)

    (out / "questions.json").write_text(
        json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 书名写进 book.json 给 layout 用：页眉必须是这本练习册的名字，
    # 不能写死成模板里那本（否则高数部分会印着「线代真题复盘」）。
    # config 里已经写了 header.title / header.book 的，直接用它——layout 那边
    # 本来也是 config 优先，再 OCR 一遍纯属白花时间（页眉窄带实测约 5 秒/页）。
    h = cfg.get("header") or {}
    if str(h.get("title") or "").strip() or str(h.get("book") or "").strip():
        book_title = str(h.get("title") or h.get("book")).strip()
    else:
        title_pages = pages
        if len(pages) < 20 and total > len(pages) * 2:
            title_pages = parse_pages(None, total)
        book_title = detect_book_title(pdf, title_pages, cfg, out)
    (out / "book.json").write_text(
        json.dumps({"title": book_title, "source": str(Path(pdf).name)}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    counts = {}
    for q in questions:
        counts[q["section"]] = counts.get(q["section"], 0) + 1
    print(f"页范围 {pages[0]}-{pages[-1]}，切出 {len(questions)} 题：{counts}")
    if catalog:
        chap = [h[3] for h in catalog if h[2] == "chap"]
        part = [h[3] for h in catalog if h[2] == "part"]
        print(f"结构：{len(part)} 个部分、{len(chap)} 个命题点")
        if part:
            print("  部分：" + "／".join(part))
        if chap:
            print("  命题点：" + "／".join(chap[:6]) + ("…" if len(chap) > 6 else ""))
    else:
        print("结构：没认出「第X部分 / 命题点」标题——做题本将只按题型分节，不出章节页。")
    if stamped:
        titles = []
        for q in questions:
            t = q.get("chapter") or ""
            if t and t not in titles:
                titles.append(t)
        print(f"章：按配置里的 chapters 盖了 {stamped} 题、{len(titles)} 章（{'／'.join(titles)}）")
    print(f"页眉书名：{book_title or '（未识别到，用文件名）'}")
    print("题号序列：" + " ".join(q["id"] for q in questions))
    return questions


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python segment_ocr.py <pdf> [config.yaml] [输出目录]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
