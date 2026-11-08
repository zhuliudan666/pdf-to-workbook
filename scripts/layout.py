"""拼版面：先把题按「每页几道」分组，再把一页的余量均分成答题区。

为什么不逐题往下流：做题本的价值在于**可预期**。第 3 页永远是 4 道选择题，
学生翻到某一页就知道该写几道、每道给多少地方。逐题流排版的结果是每页题数
随题图高低漂移（3 道、5 道、2 道），写到第 4 页才反应过来这页只放了 2 道。

所以顺序是反过来的：**先定页，再定留白**。
  1. 分组：按 page_quota 切段（选择题/填空题 4 道，解答题/证明题 3 道）。
     一页里混了大题就按 page_quota_mixed（3 道）算——3 道里既有选填又有大题时，
     留白按 3 等分，不按 4。
  2. 分配：每页的留白 = (可用高度 − Σ题图 − 题间距) ÷ 本页题数。
  3. 兜底：均分后若低于该题型的物理下限（answer_gap_floor_by_section），
     先等比缩题图（不低于 min_scale），仍不够就少放一道并报出来。

两个数字是固定的：题间距 question_gap_mm（视觉分隔）和页底呼吸位
page_bottom_breath_mm（最后一块留白不贴到页脚页码）。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import common

GREY = (0.45, 0.45, 0.45)
LIGHT = (0.78, 0.78, 0.78)
FAINT = (0.86, 0.86, 0.86)
DOT = (0.74, 0.74, 0.74)
DARK = (0.20, 0.20, 0.20)
SEC_ORDER = {"选择题": "一", "填空题": "二", "解答题": "三", "证明题": "四"}

TITLE_PART_H = 50.0    # 部分页首：「第一部分 高等数学」居中大字
TITLE_CHAP_H = 40.0    # 命题点标题 + 横线
TITLE_SEC_H = 34.0     # 题型标题（字 + 横线 + 间隔）


def usable_font(bold: bool = False) -> tuple[str, str] | None:
    path = common.cjk_font(bold=bold)
    return ("cjk", path) if path else None


def _by_section(cfg: dict, key: str, sec: str, fallback: float) -> float:
    d = cfg.get(key) or {}
    v = d.get(sec) if isinstance(d, dict) else None
    return float(v) * common.MM if v is not None else float(fallback) * common.MM


def quota_of(cfg: dict, sec: str) -> int:
    """这一题型每页放几道。0 表示没配（不限，只受高度约束）。"""
    d = cfg.get("page_quota") or {}
    v = d.get(sec) if isinstance(d, dict) else None
    return int(v) if v else 0


def page_gaps(hs: list[float], floors: list[float], caps: list[float],
              avail: float, q_gap: float, min_scale: float) -> tuple[list[float], list[float], str, float]:
    """一页之内把余量均分给每道题，返回 (图高, 留白, 状态, 缩放比)。

    ok      原样放下
    shrink  题图缩了（不低于 min_scale），换到完整留白
    tight   缩到底仍不够下限——说明这页的题确实塞不下目标题数
    cap     余量超过上限，留白封顶，多出来的白落页底（别把一道小题拉开一页）

    为什么先缩图再减题：用户要的是「这页 4 道」。少放一道会打破预期，
    缩到 0.8（300dpi 的扫描件印出来仍有 240dpi）肉眼几乎看不出。
    """
    n = len(hs)
    if n == 0:
        return [], [], "ok", 1.0
    floor = max(floors) if floors else 0.0
    cap = max(caps) if caps else 1e9
    s = sum(hs)
    scale = 1.0
    g = (avail - s - (n - 1) * q_gap) / n
    mode = "ok"

    if g < floor:
        room = avail - (n - 1) * q_gap - n * floor
        scale = max(min_scale, min(1.0, room / s)) if s > 0 else 1.0
        if scale < 0.999:
            hs = [h * scale for h in hs]
            s = sum(hs)
            g = (avail - s - (n - 1) * q_gap) / n
            mode = "shrink"
        if g < floor - 0.5:
            mode = "tight"

    if g > cap:
        g = cap
        if mode == "ok":
            mode = "cap"
    g = max(g, 0.0)
    return hs, [g] * n, mode, scale


def pack_pages(questions: list[dict], cfg: dict, hmap: dict[int, float],
               avail: float, q_gap: float, min_scale: float) -> list[dict]:
    """按「每页几道」把题分组，顺便定下每页要画哪几级标题。

    三条规矩叠在一起：
      1. **新命题点一定另起一页**。章节页是给人翻的：翻到这一页就知道
         从现在起是「2. 一元函数微分学」，而不是夹在上一章解答题中间的一行小字。
      2. 同一题型排满 page_quota 就换页（选择/填空 4 道，解答/证明 3 道）。
      3. 换题型但不换章时**不强制换页**，标题画在页中；混排页按
         page_quota_mixed（3 道）算，留白按 3 等分——否则同一页里的大题
         只能分到 1/4 页，写不下推导。

    上一版还有个隐藏 bug：判断「要不要画标题」用的是章节名有没有出现过。
    题库里每章的题型都叫「一、选择题」，于是从第二章起标题全被当成画过了，
    读者会从上一章解答题直接跳进下一章选择题，中间没有任何分界。
    现在按「段」记：只有和上一段不同才画。
    """
    mixed = int(cfg.get("page_quota_mixed", 3) or 3)

    def floor_of(sec: str) -> float:
        return _by_section(cfg, "answer_gap_floor_by_section", sec, cfg.get("answer_gap_floor_mm", 30))

    def cap_of(sec: str) -> float:
        return _by_section(cfg, "answer_gap_cap_by_section", sec, cfg.get("answer_gap_cap_mm", 120))

    def head_space(page: dict, extra_mid: bool = False) -> float:
        h = 0.0
        if page["part"]:
            h += TITLE_PART_H
        if page["chap"]:
            h += TITLE_CHAP_H
        if page["hdr"]:
            h += TITLE_SEC_H
        return h + (len(page["mids"]) + (1 if extra_mid else 0)) * TITLE_SEC_H

    def fits(page: dict, h: float, sec: str, extra_mid: bool = False) -> bool:
        """加上这一题后，按最紧的算法（缩到 min_scale、留白只给下限）还放得下吗。"""
        items = page["items"]
        n = len(items) + 1
        sumh = sum(hmap[id(q)] for q in items) + h
        fl = max(floor_of(s) for s in [q.get("section") or "" for q in items] + [sec])
        need = head_space(page, extra_mid) + sumh * min_scale + (n - 1) * q_gap + n * fl
        return need <= avail

    def used(page: dict) -> float:
        """已占高度（留白按下限估）。只用来判断标题会不会落到下半页。"""
        items = page["items"]
        sumh = sum(hmap[id(q)] for q in items)
        return head_space(page) + sumh + len(items) * floor_of(items[0].get("section") or "")

    pages: list[dict] = []
    run_part = run_chap = run_sec = None
    cur: dict | None = None

    def start(q: dict) -> dict:
        nonlocal run_part, run_chap, run_sec
        part, chap, sec = q.get("part") or "", q.get("chapter") or "", q.get("section") or ""
        # 没有题型标题的书（讲义、例题册、整本只有一套题的练习册）认不出题型，
        # 原来退回 99 = 「按空间塞」，结果 6 道短题挤在一页、每题只剩 39mm 留白，
        # 跟承诺的「大题 3 道/页」差一截。退回 mixed 档（标准档 = 3）才是那个承诺。
        p = {"part": "", "chap": "", "hdr": "", "mids": [], "items": [q],
             "sec": sec, "limit": quota_of(cfg, sec) or (mixed if not sec else 99),
             "kpart": part, "kchap": chap, "ksec": sec}
        if (part, chap) != (run_part, run_chap):
            run_chap = chap
            run_sec = ""              # 新章的第一次题型标题必须画（它也叫「选择题」）
            if part != run_part:
                # 换部分先插一整页扉页：只印「第二部分 线性代数」和本部分的命题点清单，
                # 不跟第一道题挤在一起。挤在一起就成了「页面顶上一行大字」，
                # 翻过去根本不知道换了大板块——扉页要的就是那一下停顿。
                # 认不出部分的书（没有「第X部分」这类行）不加扉页，别凭空多出空白页。
                if part:
                    pages.append({"part": part, "chap": "", "hdr": "", "mids": [], "items": [],
                                  "sec": sec, "limit": 0,
                                  "kpart": part, "kchap": "", "ksec": sec,
                                  "part_only": True})
                run_part = part
            if chap:
                p["chap"] = chap
        if sec != run_sec:
            p["hdr"] = sec
            run_sec = sec
        pages.append(p)
        return p

    for q in questions:
        sec = q.get("section") or ""
        h = hmap[id(q)]
        if cur is None:
            cur = start(q)
            continue

        new_chap = (q.get("part") or "", q.get("chapter") or "") != (cur["kpart"], cur["kchap"])
        if not new_chap:
            if sec == cur["sec"] and len(cur["items"]) < cur["limit"] and fits(cur, h, sec):
                cur["items"].append(q)
                continue
            if (sec != cur["sec"] and len(cur["items"]) < mixed
                    and fits(cur, h, sec, extra_mid=True)
                    and avail - used(cur) >= avail * 0.4):
                # 混排：标题画在页中。剩余太少时仍换页——标题挂在页面下部比换页难看。
                cur["mids"].append(len(cur["items"]))
                cur["items"].append(q)
                cur["sec"] = sec
                cur["limit"] = min(cur["limit"], mixed)
                run_sec = sec          # 页中标题已经介绍过这一段，下一页别再画页首标题
                continue

        cur = start(q)

    # 尾部平衡：一节的题数不一定是每页题数的整倍数，最后一页可能只剩 1 道，
    # 摊在 130mm 的留白上很怪（前七页每页 3 道，最后一页孤零零一道）。
    # 从上一页挪一道过来，把末两页摊平。只挪一道，且挪完两页都得放得下。
    for i in range(len(pages) - 1, 0, -1):
        p, prev = pages[i], pages[i - 1]
        if p["mids"] or prev["mids"]:
            continue
        # 扉页是空的，别把上一章的最后一题搬进扉页——那等于扉页没了
        if not p["items"] or not prev["items"]:
            continue
        if len(p["items"]) >= 2 or prev["sec"] != p["sec"]:
            continue
        # 不跨章搬：把上一章最后一题挪进下一章的章节页，章标题就跟内容对不上了
        if (p["kpart"], p["kchap"]) != (prev["kpart"], prev["kchap"]):
            continue
        if len(prev["items"]) <= len(p["items"]) + 1:
            continue
        q = prev["items"][-1]
        if not fits(p, hmap[id(q)], q.get("section") or ""):
            continue
        prev["items"].pop()
        p["items"].insert(0, q)
        p["balanced"] = prev["balanced"] = True   # 两页都因摊平而少于目标题数

    return pages


def overlay_text(page, rect, items: list[dict]) -> None:
    """在题图上盖一层不可见文字（render_mode=3）。

    这样 PDF 能搜索、能选中、能复制题干，而屏幕上看到的仍然是原书的印刷字形——
    公式不会被 OCR 猜错，因为显示的是位图，文字层只负责被检索。

    字体用 pymupdf 内建的 china-s（Heiti）：它生成的是非嵌入的 CID 字体，
    文件只多几百字节；换成 msyh.ttc 会把整个雅黑塞进 PDF，为了这层看不见的
    文字多出好几 MB，完全划不来。
    """
    for it in items:
        r = pymupdf.Rect(
            rect.x0 + it["u0"] * rect.width,
            rect.y0 + it["v0"] * rect.height,
            rect.x0 + it["u1"] * rect.width,
            rect.y0 + it["v1"] * rect.height,
        )
        size = min(13.0, max(3.0, r.height * 0.95))
        try:
            page.insert_text(
                (r.x0, r.y1 - size * 0.18),
                it["t"],
                fontname="china-s",
                fontsize=size,
                render_mode=3,
            )
        except Exception:
            continue


def _draw_gap(page, rect, gap: float, style: str, x0: float, x1: float) -> None:
    """在留白区画辅助格。

    line  一条淡横线，压在一半高度——写长式子时有个视觉中线，不撕纸面。
    dots  一格格淡点阵（竖直 8mm、水平 5mm）。点真的很多：88 题 × 一页留白
          大约多 2 万个矢量圆，文件会大 0.5–1MB。所以点用 0.45pt 半径、
          最浅的灰，打印时只是若隐若现的引导，不抢题面。
    """
    if gap <= 2:
        return
    if style == "line":
        page.draw_line(
            pymupdf.Point(x0, rect.y1 + gap * 0.5),
            pymupdf.Point(x1, rect.y1 + gap * 0.5),
            color=FAINT,
            width=0.5,
        )
        return

    step_y, step_x = 8 * common.MM, 5 * common.MM
    y = rect.y1 + step_y
    while y < rect.y1 + gap - step_y * 0.4:
        x = x0
        while x <= x1:
            page.draw_circle(pymupdf.Point(x, y), 0.45, color=DOT, fill=DOT)
            x += step_x
        y += step_y


GENERIC_STEMS = {"book", "scan", "scan001", "document", "output", "in", "input", "1", "new"}


def resolve_header(cfg: dict, out: Path, pdf: str) -> str:
    """页眉左边那行字：这本练习册的名字 + 做题本。

    取值顺序：config 显式写的 → segment_ocr 从原书页眉读出来的 → 文件名。
    写死一个名字的后果很明显：高数部分印着「线代真题复盘」，用户一眼就看出
    这是拿别人的成品套的模板，而且页眉从此不再提供任何信息。
    """
    h = cfg.get("header") or {}
    # 老 config 写的是 " · 做题本"（含前导空格）。中文字面里混一个半角空格，
    # 排出来是「课后习题 · 做题本」，中间那道缝一眼就看得出是拼的。
    # 2026-11-07 起统一成 "· 做题本"，靠这里兼容旧 config，不逼用户改 yaml。
    suffix = str(h.get("suffix", "· 做题本")).lstrip()
    if suffix:
        suffix = f" {suffix}"
    if h.get("title"):
        # 用户给的名字也照样补「 · 做题本」——页眉的语义是「这本练习册的做题本」，
        # 只印书名时读者分不清手里拿的是原书还是做题本（2024 数三那本就出过这个）。
        # 自己已经写了「做题本」的不重复加。
        t = str(h["title"]).strip()
        if suffix.strip() and "做题本" not in t:
            t = f"{t}{suffix}"
        return t                    # intake 里用户给的名字，最高优先级
    book = str(h.get("book") or "").strip()
    if not book:
        f = out / "book.json"
        if f.exists():
            try:
                book = (json.loads(f.read_text(encoding="utf-8")).get("title") or "").strip()
            except Exception:
                book = ""
    if not book:
        stem = Path(pdf).stem.strip(" _-")
        book = "" if stem.lower() in GENERIC_STEMS or len(stem) < 3 else stem
    return f"{book}{suffix}" if book else "做题本"


def main(pdf: str, cfg_path: str | None = None, out_dir: str | None = None, out_pdf: str | None = None) -> str:
    cfg = common.load_config(cfg_path)
    out = Path(out_dir or Path(pdf).parent / "workbook_out")
    questions = json.loads((out / "questions.json").read_text(encoding="utf-8"))
    if not questions:
        raise SystemExit("questions.json 是空的，先检查 config.yaml 里的 question_patterns")

    W, H = common.PAPER_SIZES[cfg["paper"].get("size", "A4")]
    m = cfg["paper"]
    # 注意：paper.margin_* 是 pt，不是毫米（留白字段另有 *_mm 后缀，别混）
    left, right = m["margin_left"], m["margin_right"]
    top, bottom = m["margin_top"], m["margin_bottom"]
    content_w = W - left - right
    content_top = top + 6
    content_bottom = H - bottom
    breath = float(cfg.get("page_bottom_breath_mm", 6)) * common.MM
    flow_bottom = content_bottom - breath
    avail = flow_bottom - content_top
    min_scale = float(cfg.get("min_scale", 0.8))
    q_gap = cfg["question_gap_mm"] * common.MM
    font = usable_font()
    header_text = resolve_header(cfg, out, pdf)

    hmap: dict[int, float] = {}
    for q in questions:
        h = content_w * q["height"] / q["width"]
        hmap[id(q)] = min(h, avail)      # 单题超一页：先缩到一页放得下

    pages = pack_pages(questions, cfg, hmap, avail, q_gap, min_scale)

    # 书签层级按这本书**实际有的**层级算，不能写死。
    # 写死成「命题点 2 级、题型 3 级」时，一本只有题型的单卷（2024 数三）
    # 书目树的第一项就从第 2 层开始，PyMuPDF 直接抛
    # 「hierarchy level of item 0 must be 1」，整本排版在最后一步失败。
    has_part = any(p.get("part") or p.get("part_only") for p in pages)
    has_chap = any(p.get("chap") or p.get("kchap") for p in pages)
    chap_l = 2 if has_part else 1
    sec_l = chap_l + 1 if has_chap else (2 if has_part else 1)
    q_l = sec_l + 1 if (has_chap or has_part) else 2

    # 每个部分下面有哪些命题点——扉页上列出来，翻到大板块时能一眼看到目录
    part_chaps: dict[str, list[str]] = {}
    for q in questions:
        p_, c_ = q.get("part") or "", q.get("chapter") or ""
        if p_ and c_ and c_ not in part_chaps.setdefault(p_, []):
            part_chaps[p_].append(c_)

    def floor_of(sec: str) -> float:
        return _by_section(cfg, "answer_gap_floor_by_section", sec, cfg.get("answer_gap_floor_mm", 30))

    def cap_of(sec: str) -> float:
        return _by_section(cfg, "answer_gap_cap_by_section", sec, cfg.get("answer_gap_cap_mm", 120))

    doc = pymupdf.open()
    bold = usable_font(bold=True) or font
    font_obj = pymupdf.Font(fontfile=font[1]) if font else None

    def text(page, rect, s, size, color, align=0, use_bold: bool = False):
        """写一行字。盒子装不下时 insert_textbox 会静默失败并返回负值，
        所以要有兜底：退化成绝对定位单行，宽度自己量。"""
        f = bold if (use_bold and bold) else font
        if not f:
            return
        rc = page.insert_textbox(
            rect, s, fontname=f[0], fontfile=f[1], fontsize=size, color=color, align=align
        )
        if rc >= 0:
            return
        w = font_obj.text_length(s, size)
        if align == 1:
            x = rect.x0 + (rect.width - w) / 2
        elif align == 2:
            x = rect.x1 - w
        else:
            x = rect.x0
        page.insert_text(
            (x, rect.y0 + size * 1.05), s, fontname=f[0], fontfile=f[1],
            fontsize=size, color=color,
        )

    def draw_title(page, y: float, sec: str, rule: bool = True) -> float:
        label = f"{SEC_ORDER.get(sec, '')}、{sec}"
        text(page, pymupdf.Rect(left, y, W - right, y + 20), label, 11.5, DARK)
        if rule:
            page.draw_line(
                pymupdf.Point(left, y + 25), pymupdf.Point(W - right, y + 25),
                color=(0.55, 0.55, 0.55), width=0.6)
        return TITLE_SEC_H

    def draw_part_page(pg, label: str, chaps: list[str]) -> None:
        """部分扉页：整页只放「第二部分 线性代数」和本部分的命题点清单。

        这一页的作用是「停顿」：200 多页里换大板块，得有一页让人知道
        「高数看完了」。所以它不跟第一道题挤在一起——挤在一起就退化成
        页面顶上一行大字，翻过去毫无感觉。
        """
        cy = H * 0.33
        text(pg, pymupdf.Rect(left, cy, W - right, cy + 32), label, 21, DARK,
             align=1, use_bold=True)
        pg.draw_line(
            pymupdf.Point(W / 2 - 72, cy + 46), pymupdf.Point(W / 2 + 72, cy + 46),
            color=(0.45, 0.45, 0.45), width=0.9)
        if not chaps:
            return
        y = cy + 86
        text(pg, pymupdf.Rect(left, y - 24, W - right, y - 8),
             f"本部分 {len(chaps)} 个命题点", 9, GREY, align=1)
        for c in chaps:
            text(pg, pymupdf.Rect(left, y, W - right, y + 18), c, 10.5,
                 (0.28, 0.28, 0.28), align=1)
            y += 21

    def draw_chap(page, y: float, label: str, rule: bool = True) -> float:
        """命题点标题，做题本里「一章」的视觉起点。"""
        text(page, pymupdf.Rect(left, y + 4, W - right, y + 24), label, 13, DARK, use_bold=True)
        if rule:
            page.draw_line(
                pymupdf.Point(left, y + 30), pymupdf.Point(W - right, y + 30),
                color=(0.42, 0.42, 0.42), width=0.9)
        return TITLE_CHAP_H

    def page_head(pg, chapter: str) -> None:
        """页眉左：这本练习册的名字 + 做题本；右：当前命题点。

        页眉原来写死在 config 里，结果高数部分印着「线代真题复盘」——
        一眼就能看出是拿别人的成品套的模板。书名现在从原书页眉里读。
        """
        text(pg, pymupdf.Rect(left, top - 30, left + 300, top - 10), header_text, 8.5, GREY)
        if chapter:
            text(pg, pymupdf.Rect(left + 300, top - 30, W - right, top - 10),
                 chapter, 8, (0.52, 0.52, 0.52), align=2)
        pg.draw_line(pymupdf.Point(left, top - 4), pymupdf.Point(W - right, top - 4),
                     color=LIGHT, width=0.5)

    toc: list[list] = []
    stats = {"short": [], "shrink": [], "tight": [], "cap": [], "balanced": []}
    scale_min = 1.0
    total_q = 0

    # ---- 目录：先算页数，再画，页码才对得上 ----
    entries: list[tuple[int, str, int]] = []
    for i, p in enumerate(pages):
        if p["part"]:
            entries.append((0, p["part"], i))
        if p["chap"]:
            entries.append((1, p["chap"], i))
    rows = max(8, int((flow_bottom - content_top - 100) / 20))
    toc_pages = math.ceil(len(entries) / rows) if (entries and cfg.get("toc", True)) else 0

    def draw_toc() -> None:
        for k in range(toc_pages):
            pg = doc.new_page(width=W, height=H)
            page_head(pg, "")
            text(pg, pymupdf.Rect(left, content_top + 6, W - right, content_top + 40),
                 "目　录", 17, DARK, align=1, use_bold=True)
            y = content_top + 62
            for lvl, label, idx in entries[k * rows:(k + 1) * rows]:
                size = 12.0 if lvl == 0 else 10.5
                x = left + (0 if lvl == 0 else 20)
                text(pg, pymupdf.Rect(x, y, W - right - 46, y + 16), label, size,
                     DARK if lvl == 0 else (0.26, 0.26, 0.26), use_bold=(lvl == 0))
                # 虚线引到页码：目录横跨 30 行又只有两级，不加引导线容易看串行
                num = str(toc_pages + idx + 1)
                if font_obj:
                    tw = font_obj.text_length(label, size)
                    dot_from = min(x + tw + 6, W - right - 54)
                    dot_to = W - right - 8 - font_obj.text_length(num, size)
                    if dot_to - dot_from > 8:
                        pg.draw_line(pymupdf.Point(dot_from, y + size * 0.8),
                                     pymupdf.Point(dot_to, y + size * 0.8),
                                     color=(0.72, 0.72, 0.72), width=0.5, dashes="[1 3] 0")
                text(pg, pymupdf.Rect(W - right - 46, y, W - right, y + 16),
                     num, size, GREY, align=2)
                y += 20
            toc.append([1, "目录", pg.number + 1])

    draw_toc()

    for page in pages:
        pg = doc.new_page(width=W, height=H)

        # 扉页：页眉右侧留空，页心只放部名和命题点清单
        if page.get("part_only"):
            page_head(pg, "")
            draw_part_page(pg, page["part"], part_chaps.get(page["part"], []))
            toc.append([1, page["part"], pg.number + 1])
            continue

        # 章的第一页页心马上要印章名，页眉右侧再印一遍就是同一行字隔两厘米重复
        # （某数三习题册那本 p3、讲义第一页都这样）。页眉只在这种页让位，其余页照旧带章名，
        # 翻到中间也还知道自己在哪一章。
        page_head(pg, "" if page["chap"] else (page.get("kchap") or page.get("kpart") or ""))

        y = content_top
        if page["chap"]:
            y += draw_chap(pg, y, page["chap"], rule=not page["hdr"])
            toc.append([chap_l, page["chap"], pg.number + 1])
        if page["hdr"]:
            y += draw_title(pg, y, page["hdr"], rule=True)
            toc.append([sec_l, f"{SEC_ORDER.get(page['hdr'], '')}、{page['hdr']}", pg.number + 1])

        items = page["items"]
        secs = [q.get("section") or "" for q in items]
        hs = [hmap[id(q)] for q in items]
        gaps_for_page = [floor_of(s) for s in secs]
        caps = [cap_of(s) for s in secs]
        hs2, gaps, mode, sc = page_gaps(hs, gaps_for_page, caps, flow_bottom - y, q_gap, min_scale)
        scale_min = min(scale_min, sc)
        target = int(cfg.get("page_quota_mixed", 3) or 3) if len(set(secs)) > 1 else (quota_of(cfg, secs[0]) or 0)
        if target and len(items) < target:
            if page.get("balanced"):
                stats["balanced"].append(pg.number + 1)
            else:
                stats["short"].append((pg.number + 1, len(items), target, secs[0]))
        if mode in stats:
            stats[mode].append(pg.number + 1)

        for i, q in enumerate(items):
            if i in page["mids"]:
                y += draw_title(pg, y, q.get("section") or "")
                toc.append([sec_l, f"{SEC_ORDER.get(q.get('section'), '')}、{q.get('section')}", pg.number + 1])
            iw, ih = q["width"], q["height"]
            h = min(hs2[i], flow_bottom - y)
            w = h * iw / ih
            rect = pymupdf.Rect(left, y, left + w, y + h)
            pg.insert_image(rect, filename=q["image"], keep_proportion=True)
            toc.append([q_l, q["id"], pg.number + 1])

            if cfg.get("text_layer", True):
                overlay_text(pg, rect, q.get("texts") or [])

            if cfg["header"]["show_source_page"]:
                text(
                    pg,
                    pymupdf.Rect(left, rect.y1 + 2, W - right, rect.y1 + 13),
                    f"原书 P{q['source_page']}",
                    6.5,
                    (0.68, 0.68, 0.68),
                    align=2,
                )
            if cfg["gap_style"] in ("line", "dots"):
                _draw_gap(pg, rect, gaps[i], cfg["gap_style"], left, W - right)
            y = rect.y1 + gaps[i] + q_gap
            total_q += 1

    total = len(doc)
    for i, pg in enumerate(doc):
        text(pg, pymupdf.Rect(W - right - 60, H - bottom + 12, W - right, H - bottom + 28),
             f"{i + 1} / {total}", 8, (0.55, 0.55, 0.55), align=2)

    # 兜底：set_toc 要求第一项必须是 1 级，且不能跳级。
    # 归一化要按**第一项**减，不能按全局最小值减：选段做「138–142 页」这种跨年切片时，
    # 第一道菜就是「一、选择题」（2 级，前面没有部分/命题点），但书里别处还有 1 级的
    # 条目，min 仍是 1，一分不减，第一项照样停在 2 级 → ValueError: hierarchy level
    # of item 0 must be 1。按第一项归一一刀切平，顺序和父子关系都不变。
    if toc:
        base = toc[0][0]
        toc = [[t[0] - base + 1, t[1], t[2]] for t in toc]
        fixed = []
        for lvl, title, pno in toc:
            if fixed:
                lvl = min(lvl, fixed[-1][0] + 1)
            fixed.append([max(1, lvl), title, pno])
        toc = fixed

    doc.set_toc(toc)
    target_path = Path(out_pdf or out / f"{Path(pdf).stem}_做题本.pdf")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.subset_fonts()
    except Exception:
        pass
    doc.save(target_path, garbage=4, deflate=True, clean=True)
    doc.close()

    mb = target_path.stat().st_size / 1e6
    print(f"输出：{target_path}（{total} 页 / {total_q} 题 / {mb:.2f}MB）")
    print("页容量：" + "、".join(
        f"{s}每页{quota_of(cfg, s)}道" for s in ("选择题", "填空题", "解答题", "证明题")
        if quota_of(cfg, s)) + f"（混排页 {cfg.get('page_quota_mixed', 3)} 道）")
    if stats["short"]:
        head = "、".join(f"P{p}（{n}/{t}）" for p, n, t, _ in stats["short"][:8])
        print(f"提示：{len(stats['short'])} 页少于目标题数：{head}"
              "——题图太高，硬塞会让答题区低于下限；想强行满页就调小 min_scale。")
    if stats["balanced"]:
        print(f"提示：{len(stats['balanced'])} 页因尾部摊平少于目标题数"
              f"（P{', P'.join(map(str, stats['balanced'][:6]))}）——一节的题数不是每页题数的整倍数，"
              "摊平比留一道孤题好看。")
    if stats["shrink"]:
        print(f"提示：{len(stats['shrink'])} 页缩过题图（最小 {scale_min:.0%}）给答题区腾地方。")
    if stats["tight"]:
        print(f"提示：{len(stats['tight'])} 页答题区仍低于下限（P{', P'.join(map(str, stats['tight'][:6]))}），"
              "这几页写起来会紧，建议换 space 档或加大纸张。")
    if stats["cap"]:
        print(f"提示：{len(stats['cap'])} 页留白已封顶，多出来的白落在页底。")
    return str(target_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python layout.py <pdf> [config.yaml] [输出目录] [输出pdf]")
    a = sys.argv
    main(a[1], a[2] if len(a) > 2 else None, a[3] if len(a) > 3 else None, a[4] if len(a) > 4 else None)
