"""成品验收：把「人工翻一遍才看得出来」的毛病变成可重复跑的检查。

为什么单独做一个文件，而不是塞进 layout 的打印语句里：
排版代码只知道自己画了什么，不知道成品读出来是什么样。页眉写错书名、目录页码
和正文对不上、某章标题压根没画——这三类错误在 layout 眼里全都是「正常执行完毕」，
只有把 PDF 读回来、按读者视角看，才发现「这本不像这本书」。

检查项与判据（每一条都来自真踩过的坑）：

1. **页眉书名统一**：取全书出现最多的那个「书名 + 做题本」当基准，逐页比对。
   写死模板名的年代，高数分册的页眉印着「线代真题复盘」，就是这么发现的。
2. **部分扉页独占一页**：带「第X部分」的页必须一张题图都没有。跟着第一道题
   挤在半页上的版本，翻过去毫无停顿感，等于没有章节页。
3. **目录页码对得上**：拿目录上印的数字去翻正文那一页，看首行是不是同一个标题。
   目录是「先算页数再画」的，一旦算错，整本页码全偏一位，而 PDF 不会报错。
4. **题图不越界**：最后一题的图底要留在可写位置之上。判「放得下」时漏算留白，
   是「页尾的题没地方写」的直接原因。
5. **答题区不塌**：相邻题图之间的竖直间隙、末题下方净空，报出最小值。

退出码 0 = 全部通过；1 = 有硬伤（页眉/目录/扉页/越界）。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pymupdf

import common

PART_RE = re.compile(r"^第\s*[一二三四五六七八九十]+\s*部分")
CHAP_RE = re.compile(r"^(\d+)\s*[.．]\s*\S")
NUM_RE = re.compile(r"^\d{1,4}$")


def _clean(s: str) -> str:
    # 页眉里的空格式子会被嵌成 NBSP，不归一化就没法和目录里的字串比
    return " ".join(s.replace("\u00a0", " ").split())


def _top_text(pg, h: float = 62.0, x1: float | None = None) -> str:
    """页眉左边那半截文字（书名），只取第一行。

    只裁左半边，是因为右边是当前命题点——右对齐、长短不一，长章名会一直延伸到
    x≈309，和左边那截在版面上几乎连成一片。按「第一行」切比按 x 切稳：
    左页眉是一次写进去的一个块，章名是另一个块，落在第二行。
    """
    rect = pymupdf.Rect(0, 0, x1 or pg.rect.width, h)
    raw = pg.get_text("text", clip=rect)
    for line in (ln for ln in raw.splitlines() if ln.strip()):
        return _clean(line)
    return ""


def _body_first(pg, y0: float = 55.0) -> str:
    """正文第一行——用来确认「目录指向的那一页，开头是不是这个标题」。"""
    for b in pg.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            s = _clean("".join(sp["text"] for sp in line["spans"]))
            if s and line["bbox"][1] >= y0:
                return s
    return ""


def _images(pg) -> list[pymupdf.Rect]:
    return sorted((pymupdf.Rect(i["bbox"]) for i in pg.get_image_info()),
                  key=lambda r: r.y0)


def _toc_pages(doc) -> list[int]:
    """目录页：正文之前、顶部有「目 录」的那几页。"""
    out = []
    for i in range(min(6, doc.page_count)):
        # 这里要整块文字，不是第一行：第一行是页眉，「目　录」在其下一行
        pg = doc[i]
        raw = _clean(pg.get_text("text", clip=pymupdf.Rect(0, 0, pg.rect.width, 130)))
        if "目" in raw and "录" in raw:
            out.append(i)
    return out


def _toc_entries(pg) -> list[tuple[str, int]]:
    """从目录页上读出 (标题, 页码)。

    标题和页码是两列，靠 y 对齐配对；页码列是纯数字，标题列带「N.」。
    用「最近的纯数字行」配对，比按行顺序数更抗 OCR/排版抖动。
    """
    labels, nums = [], []
    for b in pg.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            s = _clean("".join(sp["text"] for sp in line["spans"]))
            if not s or line["bbox"][1] < 100:
                continue
            if NUM_RE.match(s):
                nums.append((line["bbox"][1], int(s)))
            elif CHAP_RE.match(s) or PART_RE.match(s):
                labels.append((line["bbox"][1], s))
    out = []
    for y, s in labels:
        if not nums:
            break
        ny, page = min(nums, key=lambda n: abs(n[0] - y))
        if abs(ny - y) <= 4:
            out.append((s, page))
    return out


def main(pdf: str, cfg_path: str | None = None, out_dir: str | None = None) -> dict:
    cfg = common.load_config(cfg_path)
    doc = pymupdf.open(pdf)
    W, H = doc[0].rect.width, doc[0].rect.height
    m = cfg["paper"]
    # 可写位置：版心底再去掉页底呼吸位。margin_* 是 pt，不是毫米
    flow_bottom = H - m["margin_bottom"] - float(cfg.get("page_bottom_breath_mm", 6)) * common.MM

    problems: list[dict] = []
    info: list[str] = []

    # ---- 1. 页眉书名 ----
    head_x1 = m["margin_left"] + 320
    heads = []
    for i, pg in enumerate(doc):
        t = _top_text(pg, 62.0, head_x1)
        # 万一有页把章名和页眉合并到一行，切到「做题本」为止，别把它算成页眉变化
        if "做题本" in t:
            t = t.split("做题本")[0].strip() + "做题本"
        heads.append((i + 1, t))
    names = Counter(n for _, n in heads if n)
    if names:
        book, hits = names.most_common(1)[0]
        bad = [p for p, n in heads if n != book]
        info.append("页眉：%s（%d/%d 页）" % (book or "（空）", hits, doc.page_count))
        if "做题本" not in book:
            # 用户用 header.title 自定义了页眉，不带「做题本」也算合法，只提醒一句
            info.append("  页眉是自定义的，不含「做题本」——如果用 config.header.title 指定的，属正常")
        if bad:
            problems.append({"kind": "header", "detail": "页眉书名不一致的页", "pages": bad[:12]})
    else:
        problems.append({"kind": "header", "detail": "整本没有一页读到页眉", "pages": []})

    # ---- 2. 部分扉页 ----
    parts = []
    for i, pg in enumerate(doc):
        first = _body_first(pg)
        if PART_RE.match(first):
            n = len(_images(pg))
            parts.append((i + 1, first, n))
            if n:
                problems.append({"kind": "part_page", "detail":
                                 "第 %d 页是「%s」扉页，却排了 %d 道题" % (i + 1, first, n),
                                 "pages": [i + 1]})
    if parts:
        info.append("部分扉页：" + "、".join("p%d %s" % (p, t) for p, t, _ in parts))

    # ---- 3. 目录页码 ----
    toc_pages = _toc_pages(doc)
    entries: list[tuple[str, int]] = []
    for i in toc_pages:
        entries += _toc_entries(doc[i])
    checked = 0
    for label, page in entries:
        if not (1 <= page <= doc.page_count):
            problems.append({"kind": "toc", "detail": "「%s」页码 %d 超出范围" % (label, page),
                             "pages": []})
            continue
        first = _body_first(doc[page - 1])
        if PART_RE.match(label):
            ok = label.replace(" ", "") in first.replace(" ", "")
        else:
            no = CHAP_RE.match(label)
            ok = bool(no) and first.startswith(no.group(1) + ".")
        checked += 1
        if not ok:
            problems.append({"kind": "toc", "detail":
                             "目录「%s」指向 p%d，但那一页开头是「%s」" % (label, page, first[:26]),
                             "pages": [page]})
    if entries:
        info.append("目录：%d 条，逐条对过正文（%d 条正常）" % (len(entries), checked - len(
            [p for p in problems if p["kind"] == "toc"])))

    # ---- 4/5. 版面度量 ----
    hist, gaps, over, tails = Counter(), [], [], []
    for i, pg in enumerate(doc):
        imgs = _images(pg)
        hist[len(imgs)] += 1
        for a, b in zip(imgs, imgs[1:]):
            gaps.append(round(b.y0 - a.y1, 1))
        if imgs:
            last = imgs[-1].y1
            if last > flow_bottom + 1:
                over.append((i + 1, round(last - flow_bottom, 1)))
            else:
                tails.append(round(flow_bottom - last, 1))
    if over:
        problems.append({"kind": "overflow", "detail":
                         "%d 页最后一题的图底压到了可写位置以下" % len(over),
                         "pages": [p for p, _ in over[:12]]})

    # ---- 答题区是否达到本档目标 ----
    # 排版器把「这页放了几道、每题给了多少毫米」写在 layout.json 里。以前没人对账，
    # 于是「3 道/页但答题区只有 22mm」能一路跑到交付——每张题图单看都是好的，
    # 只有拿尺子量才发现写不下四行推导。
    lay = Path(out_dir) / "layout.json" if out_dir else None
    if lay is not None and lay.exists():
        try:
            log = json.loads(lay.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 记账坏了不该让验收崩掉
            log = []
        floors = cfg.get("answer_gap_floor_by_section") or {}
        fallback = float(cfg.get("answer_gap_floor_mm", 30))
        thin = []
        for rec in log:
            sec = (rec.get("sections") or [""])[0]
            want = float(floors.get(sec, fallback))
            for g in rec.get("gaps_mm") or []:
                if g + 2.0 < want:
                    thin.append((rec["page"], sec, g, want))
        if thin:
            problems.append({"kind": "answer_gap", "detail":
                             "%d 处答题区低于本档目标（最窄 p%d %s %.0fmm，目标 %.0fmm）"
                             % (len(thin), thin[0][0], thin[0][1], thin[0][2], thin[0][3]),
                             "pages": sorted({p for p, *_ in thin})[:12]})
    gaps_sorted = sorted(gaps)
    tails_sorted = sorted(tails)

    # ---- 6. 文字层：成品到底能不能搜 ----
    # 这条闸是补出来的：`layout.overlay_text` 一直有，但 `crop` 读的是
    # `ocr/page_NNN.json`，那个文件由 `ocr.ensure()` 写、全仓没人调用——
    # 于是 220 页的成品 `get_text()` 每页只剩页眉几十字，一本书都搜不到，
    # 而前面五条检查全是绿的。所以这里分两步定性：裁切时有没有存文字层，
    # 存了有没有真的印到成品上。
    qfile = Path(out_dir) / "questions.json" if out_dir else None
    if cfg.get("text_layer", True) and qfile is not None and qfile.exists():
        try:
            qs = json.loads(qfile.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            qs = []
        with_text = [q for q in qs if q.get("texts")]
        if not with_text:
            problems.append({"kind": "text_layer_missing", "detail":
                             "%d 题里没有一条文字层记录，成品将搜不到题干" % len(qs),
                             "pages": []})
        else:
            probes = []
            for q in with_text:
                best = max(q["texts"], key=lambda it: len(it.get("t") or ""))
                t = (best.get("t") or "")[:20]
                if len(t) >= 6:
                    probes.append(t)
                if len(probes) >= 5:
                    break
            found = 0
            for t in probes:
                if any(pg.search_for(t) for pg in doc):
                    found += 1
                    break          # 搜到一条就够定性：链路通了，不必全书扫完
            if probes and not found:
                problems.append({"kind": "text_layer_unprinted", "detail":
                                 "裁切存了文字层，成品里却搜不到任何一条（抽查 %d 条）" % len(probes),
                                 "pages": []})
            else:
                info.append("文字层：%d/%d 题有记录，抽查 %d 条%s"
                            % (len(with_text), len(qs), len(probes),
                               "搜到了" if found else "（无可测样本）"))

    def med(v):
        return v[len(v) // 2] if v else 0.0

    info.append("每页题数分布：%s" % dict(sorted(hist.items())))
    if gaps:
        info.append("题图间隙：最小 %.1fpt（%.0fmm）/ 中位 %.1fpt（%.0fmm）"
                    % (gaps_sorted[0], gaps_sorted[0] / common.MM,
                       med(gaps_sorted), med(gaps_sorted) / common.MM))
    if tails:
        info.append("末题下方净空：最小 %.1fpt（%.0fmm）/ 中位 %.1fpt（%.0fmm）"
                    % (tails_sorted[0], tails_sorted[0] / common.MM,
                       med(tails_sorted), med(tails_sorted) / common.MM))

    ok = not problems
    report = {"pdf": str(pdf), "pages": doc.page_count, "ok": ok,
              "book": names.most_common(1)[0][0] if names else "",
              "part_pages": [p for p, _, _ in parts],
              "toc_entries": len(entries), "problems": problems, "info": info}
    out = Path(out_dir) if out_dir else Path(pdf).parent
    (out / "accept.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    doc.close()

    print("\n".join(info))
    if problems:
        print("\n!! 验收没通过：")
        for p in problems:
            print("   [%s] %s %s" % (p["kind"], p["detail"],
                                     ("p" + ", p".join(map(str, p["pages"]))) if p["pages"] else ""))
        print("\n这些错误 layout 自己不会报——它只知道画了什么，不知道画出来像不像这本书。")
    else:
        print("\n验收通过：页眉、章节页、目录页码、版面留白都对得上。")
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python accept.py <做题本.pdf> [config.yaml] [输出目录]")
    a = sys.argv
    r = main(a[1], a[2] if len(a) > 2 else None, a[3] if len(a) > 3 else None)
    raise SystemExit(0 if r["ok"] else 1)
