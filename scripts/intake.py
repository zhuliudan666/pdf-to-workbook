"""开场引导：把「用户想要什么」翻译成「确定要跑哪几条命令」。

为什么要有这一步：这套流水线有四个入口、二十多个参数，用户不可能知道
`answer_gap_by_section` 该填什么、`pages` 该写 "12-34" 还是 [12,13]。
但如果直接问用户"你要多少毫米留白"，他也不知道。

所以分成两件事：

  1. detect() —— 能从文件里测出来的，一律不问（几页、有没有文字层、
     是不是双栏、题号正则命中多少、扫描件有效分辨率）。问了就是浪费用户时间，
     而且用户答的往往不如测的准。
  2. decide() —— 只问「测不出来、又真正改变成品形态」的决定，每个都给默认值，
     用户说"你定"就能往下走。然后把它翻译成配置文件和命令行。

三种出口（退出码）：
  0  ready        信息够了，附确认回执和执行计划
  3  needs_input  还缺几个决定，附要问的问题（这就是引导词）
  4  blocked      这个组合做不了，附原因和替代方案

用法：
    python intake.py 习题册.pdf --out 输出目录                    # 只出引导
    python intake.py 习题册.pdf --out 输出目录 --answers '{"room":"roomy"}'
    python intake.py 照片.jpg --out 输出目录 --answers answers.json

`--facts facts.json` 可以跳过探测直接喂事实，self-test 用它，不必准备真 PDF。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import common
import probe

HERE = Path(__file__).resolve().parent.parent
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

# 留白档位 = 每页放几道题。档位越紧，一页塞的题越多，每题分到的答题区越小。
# 这个数与纸型无关：A4 放 4 道选择题，B5 也还是 4 道（只是每题分到的更少）。
# gaps 只用来给回执一个「大概多少毫米」的直观参考，排版时不再读它。
# photo 两个值是 (填空, 解答) 的毫米数——照片路线不走分页配额，直接按高度给。
ROOM = {
    "compact": {
        "label": "紧凑（省纸）",
        "short": "省纸",
        "spec": "选填 5 道/页 · 大题 4 道/页",
        "quota": {"选择题": 5, "填空题": 5, "解答题": 4, "证明题": 4},
        "gaps": {"选择题": 12, "填空题": 25, "解答题": 45, "证明题": 45},
        "fallback": 40,
        "photo": (26, 48),
        "hint": "只写答案或关键步骤；大题基本写不下完整推导",
    },
    "standard": {
        "label": "标准（默认）",
        "short": "标准",
        "spec": "选填 4 道/页 · 大题 3 道/页",
        "quota": {"选择题": 4, "填空题": 4, "解答题": 3, "证明题": 3},
        "gaps": {"选择题": 18, "填空题": 33, "解答题": 62, "证明题": 62},
        "fallback": 55,
        "photo": (35, 65),
        "hint": "解答题能写下主要推导而不换页",
    },
    "roomy": {
        "label": "宽裕（草稿级）",
        "short": "草稿级",
        "spec": "选填 3 道/页 · 大题 2 道/页",
        "quota": {"选择题": 3, "填空题": 3, "解答题": 2, "证明题": 2},
        "gaps": {"选择题": 25, "填空题": 45, "解答题": 95, "证明题": 95},
        "fallback": 80,
        "photo": (45, 90),
        "hint": "要写完整证明、中间画图或反复涂改；页数比标准多约三成",
    },
}

PRINT = {
    "a4": {"label": "A4 单面（默认）", "size": "A4", "left": 66, "right": 46},
    "a4_bind": {"label": "A4 双面/装订（左边多留 15mm）", "size": "A4", "left": 81, "right": 46},
    "b5": {"label": "B5（便携小册，版心更窄）", "size": "B5", "left": 55, "right": 40},
}

STYLE = {
    "blank": "纯白（默认，最省墨）",
    "line": "一条淡中线（写长式子时对齐用）",
    "dots": "淡点阵（像方格本，逐行推导更整齐）",
}

# 用户原话 → 取值。能从第一句话里抽出来的，就不该再问一遍。
HEAR = {
    "scope": [
        ("全书|整本|整册|全部|all", "all"),
        ("试跑|先试|试试|几页|3 页|三页|看看效果", "try3"),
    ],
    "room": [
        ("紧凑|省纸|少留|简单留|短一点", "compact"),
        ("写开|够写|推导|草稿|宽裕|宽一点|宽些|多一点|大一点|详细", "roomy"),
        ("标准|正常|一般|默认|差不多", "standard"),
    ],
    "fidelity": [
        ("清晰|矢量|重排|印刷|放大|糊|虚|看不清", "vector"),
        ("位图|原样|不用重排|快一点", "bitmap"),
    ],
    "print": [
        ("b5|小册|便携", "b5"),
        ("双面|装订|打孔", "a4_bind"),
        ("a4|单面", "a4"),
    ],
    "style": [
        ("点阵|方格", "dots"),
        ("横线|中线|一条线", "line"),
        ("纯白|空白|不需要线", "blank"),
    ],
}


def hear(text: str) -> dict:
    """从用户原话里抽取值。抽不到就不填——不要猜，猜错了比不问更糟。"""
    got: dict = {}
    if not text:
        return got
    low = text.lower()
    for key, rules in HEAR.items():
        for pat, val in rules:
            if re.search(pat, low):
                got[key] = val
                break
    return got


# ---------------------------------------------------------------- 探测


def _page_dpi(page) -> int | None:
    """页面上最大那张位图的实际分辨率。扫描件整页就是一张图，量出来就是这本的清晰度。"""
    try:
        infos = page.get_image_info()
    except Exception:
        return None
    best, best_area = None, 0
    for info in infos:
        x0, y0, x1, y1 = info["bbox"]
        area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        if area > best_area and info.get("width"):
            best, best_area = info, area
    if not best:
        return None
    w_pt = best["bbox"][2] - best["bbox"][0]
    if w_pt < 1:
        return None
    return int(round(best["width"] / (w_pt / 72.0)))


def _classify(src: str) -> dict:
    """认素材类型，把预案（路线/坑/验收重点）挂到 facts 上。

    为什么不放进 detect：detect 只测「文件里有什么」，分类是「这属于哪一类」——
    后者要看 profiles/*.yaml，而且允许失败。分类挂了不能连累引导流程，
    所以整个包在 try 里：认不出来就当没有，退化成原来那套问答。
    """
    try:
        import classify

        fp = classify.fingerprint(src)
        out = classify.classify(fp)
        pres = classify.prescribe(fp, out["pick"], out["near"])
        pres["fingerprint"] = {k: v for k, v in fp.items() if k not in ("scan", "title_lines")}
        return pres
    except Exception as e:
        return {"type": "unknown", "label": "分类没跑成", "because": [f"{type(e).__name__}: {e}"],
                "pitfalls": [], "route": "", "missing": []}


def detect(src: str) -> dict:
    p = Path(src)
    if not p.exists():
        raise SystemExit(f"文件不存在：{src}")

    if p.suffix.lower() in IMAGE_EXT:
        from PIL import Image

        with Image.open(p) as im:
            w, h = im.size
        # 照片没有 dpi 元数据可依，按 A4 竖版反推：宽 210mm = 8.27in
        dpi = int(round(w / 8.27))
        ratio = h / max(w, 1)
        return {
            "input": str(p.resolve()),
            "kind": "image",
            "pages": 1,
            "text_layer": "none",
            "render_dpi": dpi,
            "pixels": [w, h],
            "a4_ratio_off": round(abs(ratio - 1.414), 2),
            "chars_per_page": 0,
            "hits_per_page": 0.0,
            "est_questions": None,
            "two_col_pages": [],
            "images_per_page": 1,
            "note": f"{w}×{h} px，按 A4 竖版折算约 {dpi}dpi",
        }

    doc = pymupdf.open(src)
    total = len(doc)
    cfg = common.load_config(str(HERE / "config.yaml"))
    chars = 0
    two_col: list[int] = []
    imgs = 0
    # 先把材料认一遍：例题册的题号是「例 1.2」，拿通用数字正则去量，
    # 命中数会是 0，然后被自己的「正则不命中」闸门挡下来——明明认出来了却报做不了。
    # 所以命中数要把预案自带的题号正则一起量。
    cls = _classify(src)
    pats = list(cfg["question_patterns"])
    for extra in ((cls or {}).get("config") or {}).get("question_patterns") or []:
        if extra not in pats:
            pats.append(extra)
    hits = {pat: 0 for pat in pats}
    for pno, page in enumerate(doc):
        txt = page.get_text("text")
        n = len("".join(txt.split()))
        chars += n
        # 单双栏也共用 probe 的判据。这里原来自己写了一遍「左右各占 28%」，
        # 结果 probe 修好后 intake 的回执照样报「1 页是双栏」——两处实现迟早对不上。
        two_col_flag, _ = probe.is_two_column(page)
        if two_col_flag:
            two_col.append(pno + 1)
        imgs += len(page.get_images(full=True))
        lines = common.page_lines(page)
        for pat in pats:
            hits[pat] += len(common.find_marks(lines, {**cfg, "question_patterns": [pat]}))

    # 三态：扫描件 + OCR 文字层会被误当成电子版，于是读它认出来的坐标，
    # 题号错位而每道题看着都对。判据和 probe.py 共用一份，别在两处各写一遍。
    chars_per_page = chars / max(total, 1)
    sig = probe.text_signals(doc)
    layer = probe.classify_text_layer(chars_per_page, **sig)
    scanned = layer != "ok"
    best = max(hits.values()) if hits else 0
    dpi = None
    if scanned:
        for page in doc:
            dpi = _page_dpi(page)
            if dpi:
                break
    doc.close()

    return {
        "input": str(p.resolve()),
        "kind": "pdf",
        "pages": total,
        "text_layer": layer,
        "text_signals": sig,
        "render_dpi": dpi,
        "chars_per_page": round(chars_per_page, 1),
        "hits_per_page": round(best / max(total, 1), 2),
        "est_questions": None if scanned else int(best),
        "two_col_pages": two_col,
        "images_per_page": round(imgs / max(total, 1), 2),
        "pattern_hits": hits,
        "cls": cls,
        "note": ("扫描件，没有文字层" + (f"，整页位图约 {dpi}dpi" if dpi else "")) if layer == "none"
        else ("扫描件 + OCR 文字层（能选中字，但字和坐标都是认出来的）"
              + (f"，整页位图约 {dpi}dpi" if dpi else "")) if layer == "ocr"
        else f"有文本层，每页 {round(chars_per_page)} 字",
    }


# ---------------------------------------------------------------- 提问


def _q(qid: str, ask: str, options: list[dict], default, why: str, blocking=True) -> dict:
    return {"id": qid, "ask": ask, "options": options, "default": default, "why": why,
            "blocking": blocking}


def questions_for(facts: dict) -> list[dict]:
    """只问「测不出来」的决定。顺序按影响大小，不是按参数字典顺序。"""
    qs: list[dict] = []

    if facts["kind"] == "image":
        qs.append(_q(
            "scope",
            "照片上是哪几道题？",
            [
                {"key": "18.3-18.11", "label": "我给你范围，如 18.3-18.11",
                 "effect": "题号前缀取自编号；不连续的写 18.3,18.5,18.7"},
                {"key": "18.3-18.11 / 填空 18.3-18.7", "label": "连同「哪些是填空」一起给",
                 "effect": "格式：18.3-18.11，填空 18.3-18.7；不给就把所有题都按解答留白"},
            ],
            None,
            "照片里没有可靠的题号序列可自动推断，切几道题只能由你说",
        ))
    elif (facts.get("cls") or {}).get("type") not in ("exam-paper", "bank-book"):
        # 真题卷/题库这两类整本都是题，范围就是全书——不问了，回执里报一句
        qs.append(_q(
            "scope",
            "做到哪一部分？",
            [
                {"key": "all", "label": "全书（默认）",
                 "effect": "整本切完；页数多时一次跑完大约几分钟"},
                {"key": "try3", "label": "先做前 3 页试跑",
                 "effect": "1 分钟内出结果，先确认切点对不对，再放开跑全书"},
                {"key": "pages", "label": "指定页码范围",
                 "effect": "回复如 12-34；只做某一讲时用这个，省掉大半 OCR 时间"},
            ],
            "all",
            "「只做第 18 讲」这类范围意图，文件里测不出来",
        ))

    # 选项名必须自己说清「怎么排」，不能只给「紧凑/标准/宽裕」这种档位名。
    # 用户是先看标题再决定要不要展开副行的；标题里只有形容词，他就得先问一句
    # 「标准到底是几道」，等于这一问白问。所以：**参数进标题，适用场景进副行**。
    #
    # 标题要短到能塞进一行选项：实测被卡片显示成
    # 「compact 选填 5 道/页 · 大题 4 道/页 | 紧凑（省纸）」——key 混进来了、
    # 「道/页」重复四遍、还叠了两个档位名。现在是「选填 5 · 大题 4（省纸）」，14 个字，
    # 数字仍在最前面的两个词里，怎么截都丢不了。
    qs.append(_q(
        "room",
        # 题干和选项必须是同一件事：选项写的是「5 选填 · 4 大题」，
        # 题干就不能问「每题留多少空白」——那是同一件事的另一种说法，
        # 摆在一起会让用户以为还有别的维度要定（实测被这么指出来过）。
        "一页排几道题？",
        [
            {"key": k,
             # 「/页」必须挂在每个数字上。写成「5 选填 · 4 大题」会被读成
             # 「一页 5 道选填 加 4 道大题 = 9 道」——实测被这么理解过。
             # 加了单位，两种题型就是两个互斥的档，加法读法自然消失。
             "label": "选填 %d/页 · 大题 %d/页（%s）"
                      % (ROOM[k]["quota"]["选择题"], ROOM[k]["quota"]["解答题"],
                         ROOM[k]["short"]),
             "effect": "选填页 %d 道、大题页 %d 道（一页里混着两种题时按 %d 道算）；"
                       "答题区 选择 %d / 填空 %d / 解答 %d mm。%s"
                       % (ROOM[k]["quota"]["选择题"], ROOM[k]["quota"]["解答题"],
                          ROOM[k]["quota"]["解答题"],
                          ROOM[k]["gaps"]["选择题"], ROOM[k]["gaps"]["填空题"],
                          ROOM[k]["gaps"]["解答题"], ROOM[k]["hint"])}
            for k in ("compact", "standard", "roomy")
        ],
        "standard",
        "答题区的宽窄全看这一项；改它不用重新切题，但整本要重排一遍",
    ))

    # 只有真的糊到影响打印时才问清晰度。分辨率够就直接定，不拿这个问题烦人。
    # 170 这条线是实测出来的：186dpi 的扫描件印出来可接受，155dpi 的手机照片被退回。
    if facts["kind"] == "image" or (facts.get("render_dpi") or 999) < 170:
        dpi = facts.get("render_dpi")
        qs.append(_q(
            "fidelity",
            "清晰度要哪一种？",
            [
                {"key": "bitmap", "label": "位图裁切（默认）",
                 "effect": f"原样保留，几分钟出结果；按 {dpi}dpi 打印会有点发虚，但能读"},
                {"key": "vector", "label": "矢量重排",
                 "effect": "印刷级清晰；每题 30–60 秒转写 + 逐题核对，20 题以上要按小时算"},
            ],
            "bitmap",
            f"这份输入只有约 {dpi}dpi，位图撑不住「放大看」，而这个代价差一个数量级，必须你定",
        ))

    return qs


def optional_defaults(facts: dict) -> list[dict]:
    """不阻塞的项：给默认值，只在回执里报一句，用户想改再说。"""
    return [
        _q("print", "纸张与装订", [{"key": k, "label": v["label"]} for k, v in PRINT.items()],
           "a4",
           "没打印机信息就当 A4 单面", blocking=False),
        _q("style", "留白样式", [{"key": k, "label": v} for k, v in STYLE.items()],
           "blank", "纯白最省墨，线格只是辅助", blocking=False),
        _q("annot", "每题标原书页码", [{"key": True, "label": "标（默认）"},
                                      {"key": False, "label": "不标"}],
           True, "回查原书时有用；纯练习不需要", blocking=False),
    ]


# ---------------------------------------------------------------- 判定


def _norm_scope(v):
    """scope 允许四种写法：全书的 key、try3、页码串、dict。统一成 (mode, spec)。"""
    if v in (None, "", "all"):
        return ("all", None)
    if v in ("try3", "try"):
        return ("try", "1-3")
    if isinstance(v, dict):
        return ("pages", str(v.get("pages") or "")) if v.get("pages") else ("all", None)
    return ("pages", str(v))


def _parse_photo_scope(v, facts) -> tuple[list[str], list[str], dict]:
    """照片的范围：返回 (题号列表, 填空列表, 提示)。"""
    if not v:
        return [], [], {}
    if isinstance(v, dict):
        prefix = str(v.get("prefix") or "").strip()
        spec = str(v.get("range") or v.get("scope") or "").strip()
        fill = str(v.get("fill") or "").strip()
    else:
        prefix, spec, fill = "", str(v).strip(), ""
        m = re.match(r"^(\d+)\.", spec)
        if m:
            prefix = m.group(1)

    def expand(s: str, prefix: str) -> list[str]:
        out: list[str] = []
        for chunk in re.split(r"[,,、\s]+", s):
            chunk = chunk.strip()
            if not chunk:
                continue
            m = re.match(r"^(\d+)\.(\d+)\s*-\s*(?:(\d+)\.)?(\d+)$", chunk)
            if m:
                pre = m.group(1) or prefix
                a, b = int(m.group(2)), int(m.group(4))
                out.extend(f"{pre}.{i}" for i in range(a, b + 1))
                continue
            if re.match(r"^\d+\.\d+$", chunk):
                out.append(chunk)
        return out

    items = expand(spec, prefix)
    fills = expand(fill, prefix) if fill else []
    return items, fills, {"prefix": prefix or (items[0].split(".")[0] if items else ""),
                          "range": spec, "fill": fill}


def decide(facts: dict, answers: dict | None = None, has_input: bool | None = None,
           said_keys: set[str] | None = None) -> dict:
    """把事实 + 答案变成计划。这是这个脚本唯一带判断力的部分。

    两段式：没有用户输入时先出引导（哪怕每项都有默认值，也要先让用户看一眼）；
    一旦用户开了口，没提到的项直接吃默认值——**回执代替追问**，
    而不是把人按在问卷上逐项填。只有「没有默认值」的那几项才会真的拦住。
    """
    answers = dict(answers or {})
    if has_input is None:
        has_input = bool(answers)
    qs = questions_for(facts)
    opt = optional_defaults(facts)

    unanswered = [q for q in qs if answers.get(q["id"]) in (None, "")]
    hard = [q for q in unanswered if q["default"] is None]
    asking = hard if has_input else unanswered

    res: dict = {
        "heard": {k: answers[k] for k in (said_keys or ()) if answers.get(k) is not None},
        "facts": facts,
        "answers": answers,
        "warnings": [],
        "blocked": [],
        "questions": asking,
        "optional": opt,
        "status": "needs_input" if asking else "ready",
    }
    if asking:
        res["why_missing"] = "；".join(f"{q['ask']}" for q in asking)
        return res

    # ---- 拿回复合值，缺的用默认 ----
    for q in qs + opt:
        if answers.get(q["id"]) in (None, ""):
            answers[q["id"]] = q["default"]

    room_key = answers["room"] if answers["room"] in ROOM else "standard"
    room = ROOM[room_key]
    printing = PRINT.get(answers.get("print") or "a4", PRINT["a4"])
    paper = common.PAPER_SIZES[printing["size"]]
    max_h = paper[1] - 58 - 46 - 6

    # ---- 硬性拦阻：这几条不解决，后面全白做 ----
    if facts["kind"] == "pdf":
        if facts["two_col_pages"]:
            res["blocked"].append(
                f"{len(facts['two_col_pages'])} 页是双栏（如 p{facts['two_col_pages'][0]}）。"
                "切点按整页行墨量算，双栏会把左右两栏搅在同一行里，切出来的题必然错位。"
            )
        if facts["text_layer"] == "ok" and facts["hits_per_page"] < probe.MIN_HITS_PER_PAGE:
            res["blocked"].append(
                f"题号正则平均每页只命中 {facts['hits_per_page']} 题。"
                "先补一条贴合这本排版的正则（看 probe.md 的命中表），否则切点算法再准也是切错位置。"
            )

    scope_mode, spec = _norm_scope(answers.get("scope"))
    if facts["kind"] == "pdf":
        if scope_mode in ("pages", "try"):
            pages = common.parse_pages(spec, facts["pages"])
            if not pages:
                res["blocked"].append(f"页码范围 {spec} 落在这本（共 {facts['pages']} 页）之外。")
            elif len(pages) > facts["pages"] * 0.9:
                res["warnings"].append(
                    f"你选的范围覆盖了 {len(pages)}/{facts['pages']} 页，等于全书；"
                    "直接按全书跑就行。"
                )

    photo = {}
    if facts["kind"] == "image" and not res["blocked"]:
        items, fills, photo = _parse_photo_scope(answers.get("scope"), facts)
        if not items:
            res["blocked"].append(
                "没解析出题号范围。照片必须给出题号（如 18.3-18.11），"
                "因为整页 OCR 在这类版面会漏识题号，流水线靠你给的编号切题。"
            )
        else:
            step = room["photo"]
            bad = [s for s in fills if s not in items]
            if bad:
                res["warnings"].append(
                    f"填空列表里的 {bad[:3]} 不在题目范围内，会被忽略。"
                )
            if not [s for s in fills if s in items]:
                res["warnings"].append(
                    "没指定哪些是填空，全部按解答题留白给——更宽松也更费纸。"
                    "想分档就说「填空是 18.3-18.7」。"
                )
            photo.update({"items": items, "fills": [s for s in fills if s in items],
                          "gap_fill": step[0], "gap_solve": step[1]})
            if len(items) > 30:
                res["warnings"].append(
                    f"一张照片切 {len(items)} 题，题间距会压到很小，切点容易互相吃。"
                    "建议按页分批拍、分批做。"
                )

    # ---- 清晰度：三条路线的判断不一样 ----
    fidelity = answers.get("fidelity") or "bitmap"
    if fidelity == "vector":
        if facts["kind"] == "pdf" and facts["text_layer"] == "ok":
            # 有文本层的 PDF 本身就是矢量，转写毫无意义，只会引入识别错误
            res["warnings"].append(
                "这本有文本层，源头就是矢量字。不用转写——把 paper.dpi 提到 400 直接渲染，"
                "清晰度已经封顶，转写只会平白引入识别错误。已按位图 400dpi 处理。"
            )
            answers["fidelity"] = "bitmap"
            answers["dpi"] = 400
        elif facts["kind"] == "pdf" and (facts.get("render_dpi") or 0) >= 240:
            res["warnings"].append(
                f"原书扫描件已经有 {facts['render_dpi']}dpi，位图直接印就够清晰了。"
                "转写的收益抵不上核对成本，已按位图处理。"
            )
            answers["fidelity"] = "bitmap"
        else:
            n = facts.get("est_questions") or (len(photo.get("items") or []) or None)
            if n and n > 12:
                res["warnings"].append(
                    f"矢量重排要逐题转写再逐题核对，{n} 题大约 {n * 45 // 60}–{n} 分钟，"
                    "而且长任务里遗漏概率会上升。建议先做一小节验收，再决定要不要全做。"
                )
            res["warnings"].append(
                "矢量重排的失败是「认错了但看起来完全正常」，所以每题拿不准的符号必须写进 "
                "items.json 的 uncertain，否则校对单里没有痕迹。"
            )

    # ---- 留白 vs 版心 ----
    # 现在留白是「版心高 ÷ 每页题数」，所以真正要检的是这张纸够不够放下这一页。
    q_big = max(1, int(room["quota"].get("解答题", 3)))
    per_mm = (max_h / common.MM) / q_big
    if per_mm < 50:
        res["warnings"].append(
            f"{printing['label']} 的版心高 {max_h:.0f}pt（{max_h / common.MM:.0f}mm），"
            f"一页排 {q_big} 道解答题后每题只剩 {per_mm:.0f}mm 答题区，写推导会很挤。"
            "换 A4、或把留白档位调成紧凑（每页少排一道）。"
        )

    if facts["kind"] == "pdf" and facts["text_layer"] == "ocr":
        res["warnings"].append(
            "这本是**扫描件 + OCR 文字层**：能选中、能搜，但那层字是认出来的。"
            "整页图占 "
            f"{(facts.get('text_signals') or {}).get('image_cover', 0):.0%}，"
            f"平均每个文本块只有 {(facts.get('text_signals') or {}).get('chars_per_span', 0):.1f} 个字。"
            "所以题号不从它读，改走条带 OCR；文字层只留给成品做检索影子。"
        )

    if facts["kind"] == "pdf" and facts["text_layer"] in ("none", "ocr") and scope_mode == "all":
        sec = facts["pages"] * 2.6
        res["warnings"].append(
            f"扫描件全书 OCR：{facts['pages']} 页，题号条带识别大约 {sec / 60:.0f} 分钟"
            "（有缓存，重跑只处理新增页）。先跑 try3 能省掉一次白等。"
        )

    if facts["kind"] == "image" and facts.get("a4_ratio_off", 0) > 0.18:
        res["warnings"].append(
            f"这张图的宽高比是 {facts['pixels'][0]}×{facts['pixels'][1]}，不像单页 A4。"
            "可能拍到了跨页、或者只拍了半页——切点会落在奇怪的位置，建议重拍。"
        )

    if res["blocked"]:
        res["status"] = "blocked"
        return res

    # ---- 生成配置与命令 ----
    cfg = common.load_config(str(HERE / "config.yaml"))

    # 认出来的素材类型可以自带一份配置片段（例题的正则、切点停在哪）。
    # 不套这一段，「认出来了」跟「用得上」之间还隔着手抄正则——
    # 而手抄最容易抄错的就是 ^ 和转义。
    cls_cfg = (facts.get("cls") or {}).get("config") or {}
    for k, v in cls_cfg.items():
        if k in ("question_patterns",):
            # 题号长什么样由这类素材说了算。叠加基础数字正则，正文里的「1.」会被当成题号。
            cfg[k] = list(v)
        elif isinstance(v, list) and isinstance(cfg.get(k), list):
            # 其余都是「再加几条」：页码、书脚、水印这些基础忽略项不能丢，
            # 丢了页码会变成题号。
            cfg[k] = list(cfg[k]) + [x for x in v if x not in cfg[k]]
        else:
            cfg[k] = v
    cfg["page_quota"] = dict(room["quota"])
    # 混排页跟着「大题」走：一页里既有选填又有大题时，按本档的大题数算。
    # 固定 3 只对标准档成立——宽裕档还排 3 道，那道大题就只能分到 1/3 页，
    # 跟「草稿级留白」自相矛盾；紧凑档排 3 道又白浪费了省下来的空间。
    cfg["page_quota_mixed"] = int(room["quota"].get("解答题") or 3)
    cfg["gap_style"] = answers.get("style") or "blank"
    # 有文本层：源头就是矢量，400dpi 是白拿的清晰度；扫描件原生 dpi 已经是上限，
    # 调高只是插值放大，除了变胖没有收益。
    render_dpi = int(answers.get("dpi") or (400 if facts["text_layer"] == "ok" else 300))
    cfg["paper"] = {
        "size": printing["size"],
        "margin_top": 58,
        "margin_bottom": 46,
        "margin_left": printing["left"],
        "margin_right": printing["right"],
        "dpi": render_dpi,
    }
    cfg["header"] = dict(cfg["header"])
    cfg["header"]["show_source_page"] = bool(answers.get("annot", True))
    if answers.get("title"):
        cfg["header"]["title"] = str(answers["title"])
    if facts["kind"] == "pdf" and scope_mode in ("pages", "try"):
        cfg["pages"] = spec

    plan: dict = {"route": "", "commands": [], "outputs": [], "rooms": room["quota"]}

    if facts["kind"] == "image":
        plan["route"] = "photo"
        items = photo["items"]
        fills = photo["fills"]
        stem = Path(facts["input"]).stem
        out_pdf = str(Path(answers.get("out") or ".") / (answers.get("name") or f"{stem}_做题本.pdf"))
        plan["commands"] = [{
            "step": "照片单页流水线",
            "cmd": (f'photo_pipeline.py --src "{facts["input"]}" --prefix {photo["prefix"] or "1"} '
                    f'--fill {",".join(fills)} --gap-fill {photo["gap_fill"]} '
                    f'--gap-solve {photo["gap_solve"]} --out "{out_pdf}"'),
        }]
        plan["outputs"] = [out_pdf]
        plan["estimate"] = {"questions": len(items), "pages": "2–4"}
        plan["photo_items"] = items
        plan["photo_fills"] = fills
        plan["note"] = "照片路线没有校对单，交付前必须看一眼渲染出的 PNG。"
    else:
        out_dir = answers.get("out") or "workbook_out"
        cfg_path = str(Path(out_dir) / "intake.yaml")
        plan["config_path"] = cfg_path
        plan["route"] = "segment" if facts["text_layer"] == "ok" else "segment_ocr"
        pdf = facts["input"]
        if plan["route"] == "segment":
            plan["commands"] = [{
                "step": "一键全流程（体检→切题→裁图→结构校验→校对单→拼版）",
                "cmd": f'build.py "{pdf}" "{cfg_path}" "{out_dir}"',
            }]
        else:
            # build.py 自己按 text_layer 选引擎，扫描件也能一条命令跑完。
            # 分步命令不是替代品，是排查时的下钻路径——题切歪了、少了一页，
            # 得能单独重跑某一步而不重跑全书 OCR，所以两条都列，顺序上先给省事的。
            plan["commands"] = [
                {"step": "一键全流程（体检→切题→裁图→结构校验→校对单→拼版→验收）",
                 "cmd": f'build.py "{pdf}" "{cfg_path}" "{out_dir}"'},
                {"step": "—— 以下为分步，排查时用 ——", "cmd": ""},
                {"step": "切题（OCR 找题号）", "cmd": f'segment_ocr.py "{pdf}" "{cfg_path}" "{out_dir}"'},
                {"step": "裁题图", "cmd": f'crop.py "{pdf}" "{cfg_path}" "{out_dir}"'},
                {"step": "结构校验（题号连续性）",
                 "cmd": f'structural.py "{out_dir}/questions.json" --json "{out_dir}/structural.json"'},
                {"step": "校对单", "cmd": f'review.py "{pdf}" "{cfg_path}" "{out_dir}"'},
                {"step": "拼版面", "cmd": f'layout.py "{pdf}" "{cfg_path}" "{out_dir}" '
                                        f'"{out_dir}/做题本.pdf"'},
                # 验收必须列进来。页眉印错书名、目录整本偏一位、章节页没画，
                # layout.py 全都视为「正常执行完毕」，只有把 PDF 读回来才发现。
                {"step": "成品验收（页眉/扉页/目录页码/越界/留白）",
                 "cmd": f'accept.py "{out_dir}/做题本.pdf" "{cfg_path}" "{out_dir}"'},
            ]
        plan["commands"].append({
            "step": "切点自检 + 诊断（最后一关，红了一定要跑诊断再改参数）",
            "cmd": (f'selfcheck.py "{pdf}" "{out_dir}" "{cfg_path}" --json "{out_dir}/selfcheck.json"'
                    f' && diagnose.py --check "{out_dir}/selfcheck.json" '
                    f'--questions "{out_dir}/questions.json"'),
        })
        plan["outputs"] = [f"{out_dir}/做题本.pdf", f"{out_dir}/校对单.pdf"]
        n = facts.get("est_questions")
        if n and scope_mode in ("all",):
            # 页数 = 题数 ÷ 平均每页题数。选填和大题配额不同，取两者均值近似。
            per = (room["quota"].get("选择题", 4) + q_big) / 2.0
            plan["estimate"] = {"questions": n, "pages": math.ceil(n / max(per, 0.5))}
        elif n:
            plan["estimate"] = {"questions": f"按 {spec} 页估计", "pages": "—"}
        elif scope_mode in ("pages", "try"):
            # 只跑几页时给个量级，别给假精确：扫描件没切完就不知道准数。
            # 密度按每页 4–7 题估——真题分类册实测 6.8 题/页，早期写死的 2–3 题
            # 是照文科习题册猜的，报出来少了一半多，用户拿着这个数判断要不要跑全书会跑偏。
            npx = len(common.parse_pages(spec, facts["pages"]))
            plan["estimate"] = {"questions": f"{npx * 4}–{npx * 7}（估）", "pages": "—"}

    res["plan"] = plan
    res["config"] = cfg
    return res


# ---------------------------------------------------------------- 文本


def _fact_line(facts: dict) -> str:
    if facts["kind"] == "image":
        dpi = facts.get("render_dpi")
        return (f"一张 {facts['pixels'][0]}×{facts['pixels'][1]} 的照片（约 {dpi}dpi）。"
                "照片要先做光照归一化和去斜，题号靠你给的编号切。")
    bits = [f"{facts['pages']} 页"]
    if facts["text_layer"] == "none":
        bits.append(f"扫描件、没有文字层（整页位图约 {facts.get('render_dpi')}dpi），题号要 OCR 识别，比电子版慢")
    elif facts["text_layer"] == "ocr":
        bits.append(
            f"扫描件加了 OCR 文字层（整页位图约 {facts.get('render_dpi')}dpi）——"
            "文字能选中，但那是认出来的，题号不从那层读，照样走条带 OCR"
        )
    else:
        bits.append(f"有文本层，每页约 {round(facts['chars_per_page'])} 字，题号直接读坐标")
        if facts.get("est_questions"):
            bits.append(f"题号正则命中 {facts['est_questions']} 题")
    if facts["two_col_pages"]:
        bits.append(f"其中 {len(facts['two_col_pages'])} 页是双栏")
    return "，".join(bits) + "。"


def render_md(res: dict) -> str:
    facts = res["facts"]
    md = ["## 我看到什么", "", _fact_line(facts), ""]

    cls = facts.get("cls") or {}
    if cls.get("type") and cls["type"] != "unknown":
        md += ["## 这是哪一类素材", "",
               f"**{cls['label']}**（`{cls['type']}`）—— 判据：{'；'.join(cls.get('because') or [])}", ""]
        if cls.get("route"):
            md.append(f"- 走法：{cls['route']}")
        if cls.get("config"):
            # 说清楚「我替你改了什么」：用户翻 intake.yaml 时会看到陌生的正则，
            # 不解释的话，下次换本书他不敢动这一行。
            what = {"question_patterns": "题号正则", "cut_stop_patterns": "切点停在哪",
                    "ignore_patterns": "要忽略的行"}
            md.append("- 已按这类素材改好配置："
                      + "、".join(f"`{k}`（{what.get(k, k)}）" for k in cls["config"]))
        if cls.get("accept"):
            md.append(f"- 验收重点：{cls['accept']}")
        if not cls.get("verified"):
            md += ["", "> ⚠ 这条预案还没被真书验证过，先用前 3 页试跑确认切点。"]
        if cls.get("pitfalls"):
            md += ["", "这类书踩过的坑（已经写进流程，不是提醒你去注意）：", ""]
            md += [f"- {x}" for x in cls["pitfalls"]]
        md.append("")
    elif cls.get("missing"):
        md += ["## 这是哪一类素材", "",
               "没匹配到预案（最接近的一类差在：" + "；".join(cls["missing"]) + "）。按通用流程跑。", ""]

    if res["status"] == "needs_input":
        md += ["## 需要你定的", ""]
        for i, q in enumerate(res["questions"], 1):
            md.append(f"**{i}. {q['ask']}**")
            md.append("")
            for o in q["options"]:
                effect = o.get("effect")
                # 顺序是「标签 → 说明 → 值」：实测把 key 放行首时，转述的人会连
                # `compact` 一起抄进选择卡片的标题里，变成
                # 「compact 选填 5 道/页 · 大题 4 道/页 | 紧凑（省纸）」。
                # 标签是给人看的，key 是给 --answers 用的，别让前者背后者。
                md.append(f"- {o['label']}"
                          + (f"　—　{effect}" if effect else "")
                          + f"　〔值：`{o['key']}`〕")
            if q["default"] is not None:
                md.append(f"- 不回就按默认：`{q['default']}`")
            else:
                md.append("- 这一项没有默认值，必须你给")
            md.append(f"- 为什么问：{q['why']}")
            md.append("")
        md += ["## 其余我按默认来", ""]
        for q in res["optional"]:
            cur = next((o for o in q["options"] if o["key"] == q["default"]), None)
            md.append(f"- {q['ask']}：{cur['label'] if cur else q['default']}")
        md += ["", "## 怎么回答", ""]
        if facts["kind"] == "image":
            md.append("一句话就行，例如「18.3-18.11，填空是 18.3-18.7，留白宽一点」。"
                      "范围必须给，其余不回就按默认。")
        else:
            md.append("一句话就行，例如「全书，留白宽一点，A4」。"
                      "不想逐项定就回一句「你定」，我按上面的默认值跑，跑之前会先跟你核对一遍。")
        md.append("")
        return "\n".join(md)

    if res["status"] == "blocked":
        md += ["## 这本做不了，先别跑", ""]
        for b in res["blocked"]:
            md.append(f"- {b}")
        md += ["", "## 能怎么办", ""]
        blob = " ".join(res["blocked"])
        if "双栏" in blob:
            md.append("- 双栏：先把 PDF 拆成单栏（或用别的工具重排）再回来。")
        if "正则" in blob:
            md.append("- 题号正则不命中：看 `probe.md` 的命中表，把贴合的一条写进 `config.yaml` 的 `question_patterns`。")
        if "页码" in blob:
            md.append(f"- 页码越界：这本共 {res['facts']['pages']} 页，对一下你给的范围。")
        if "题号范围" in blob:
            md.append("- 照片必须给题号，例如「18.3-18.11」；不连续就逐个列，如「18.3,18.5,18.7」。")
        md.append("- 只想先看效果：先指定几页试跑，把切点调对再放开。")
        md.append("")
        return "\n".join(md)

    a = res["answers"]
    plan = res["plan"]
    cfg = res["config"]
    room = ROOM.get(a["room"], ROOM["standard"])
    md += ["## 确认一下，我要这么做", ""]
    if plan["route"] == "photo":
        pis = plan.get("photo_items") or []
        scope_txt = f"{pis[0]}–{pis[-1]}（{len(pis)} 题）" if pis else "照片"
    else:
        scope_txt = "全书" if not cfg.get("pages") else f"第 {cfg['pages']} 页"
    md.append(f"- 范围：{scope_txt}")
    est = plan.get("estimate") or {}
    if est.get("questions"):
        tail = f"，成品约 {est['pages']} 页" if est.get("pages") not in (None, "—") else ""
        md.append(f"- 题量：约 {est['questions']} 题{tail}")
    md.append(f"- 每页题数：{room['spec']}"
              f"（答题区 选择 {room['gaps']['选择题']} / 填空 {room['gaps']['填空题']} / "
              f"解答 {room['gaps']['解答题']} mm；混排页按 {room['quota']['解答题']} 道）")
    md.append(f"- 纸：{PRINT.get(a.get('print') or 'a4', PRINT['a4'])['label']}，"
              f"留白样式 {STYLE.get(a.get('style') or 'blank', '纯白')}")
    md.append(f"- 清晰度：{'矢量重排（逐题转写）' if a.get('fidelity') == 'vector' else '位图裁切'}"
              f"{'，渲染 400dpi' if cfg['paper']['dpi'] >= 400 else ''}")
    heard = res.get("heard") or {}
    if heard:
        md.append("")
        md.append("> 其中 " + "、".join(f"`{k}`={v}" for k, v in heard.items())
                  + " 是从你原话里读出来的。读错了说一声，我改。")
    md.append("")
    if res["warnings"]:
        md += ["## 要提前知道的", ""]
        for w in res["warnings"]:
            md.append(f"- {w}")
        md.append("")
    md += ["## 会跑这些", ""]
    for i, c in enumerate(plan["commands"], 1):
        md.append(f"{i}. {c['step']}")
    md.append("")
    md.append("产物：" + "、".join(os.path.normpath(str(x)) for x in plan["outputs"]))
    if plan.get("note"):
        md.append(f"注意：{plan['note']}")
    md.append("")
    return "\n".join(md)


# ---------------------------------------------------------------- 入口


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="开局引导：facts -> 问题 -> 计划")
    ap.add_argument("src", help="习题册 PDF 或书页照片")
    ap.add_argument("--out", help="输出目录（同时决定 intake.yaml 的位置）")
    ap.add_argument("--answers", help="答案 JSON 文件，或直接内联 JSON 串")
    ap.add_argument("--say", help="用户原话，从中抽取能确定的值（抽不到就不填）")
    ap.add_argument("--facts", help="直接喂事实 JSON，跳过探测（自测用）")
    ap.add_argument("--defaults", action="store_true", help="用户说「你定」：全部按默认值跑")
    args = ap.parse_args(argv)

    if args.facts:
        facts = json.loads(Path(args.facts).read_text(encoding="utf-8"))
    else:
        facts = detect(args.src)
        # detect 里已经认过一次（要拿预案的正则去量命中数），别重复跑指纹
        facts["cls"] = facts.get("cls") or _classify(args.src)

    answers: dict = {}
    said: dict = {}
    if args.say:
        said = hear(args.say)
        answers.update(said)
    if args.answers:
        raw = args.answers.strip()
        answers.update(json.loads(raw) if raw.startswith("{") else
                       json.loads(Path(raw).read_text(encoding="utf-8")))
    if args.out:
        answers.setdefault("out", args.out)

    has_input = bool(said) or bool(args.answers) or args.defaults
    res = decide(facts, answers, has_input=has_input, said_keys=set(said))
    md = render_md(res)

    out = Path(args.out) if args.out else Path.cwd()
    out.mkdir(parents=True, exist_ok=True)
    (out / "intake.json").write_text(json.dumps(res, ensure_ascii=False, indent=2,
                                                default=str), encoding="utf-8")
    (out / "intake.md").write_text(md, encoding="utf-8")

    if res["status"] == "ready" and res.get("config") is not None and res["facts"]["kind"] == "pdf":
        import yaml

        cfg_path = out / "intake.yaml"
        cfg_path.write_text(
            "# 由 intake.py 生成，改了答案重跑一次即可覆盖\n"
            + yaml.safe_dump(res["config"], allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    print(md)
    return {"ready": 0, "needs_input": 3, "blocked": 4}[res["status"]]


if __name__ == "__main__":
    sys.exit(main())
