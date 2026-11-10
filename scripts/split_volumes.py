"""合订卷预处理：把「上一卷末题 + 下一卷卷标题」挤在同一页的输入拆成干净页段。

为什么需要这个工具（不是脚本能自动绕过去的）：多套合订时每套题号都从 1 重排，
`chapters` 按整页盖章。卷次交界落在页中间时，两个页段必然重叠，谁先谁后
都会有一半的题盖错章，题号「22→1」被判往回走删掉（15 页实测只切出 40 题）。

做法是输入侧的账，不改判据：
  1. 读目录页，去掉公共书名前缀，得到每卷的短名（第一套试题 / 真题精选卷…）
     认不出就停，不猜；可用 --titles 手工给。
  2. 正文里找含短名的居中标题行。标题上方还有 ≥3 行正文 ⇒ 这页要拦腰拆开。
  3. 上半保留原坐标；下半丢掉卷标题横条与页脚，整体上移成正常卷首页。
     两页都仍是 A4、内容 1:1 不缩放，后面的 content_top / 页脚判据照常生效。
  4. 输出新 PDF、split.json（拆页依据），可选 --patch 把 chapters 写进配置。

用法：
  python scripts/split_volumes.py 书.pdf --out 书_分册.pdf \
      --patch out/intake.yaml --record out/split.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import ocr  # noqa: E402 —— 扫描件整页 OCR（引擎惰性初始化）

TOC_WORD = re.compile(r"目[\s\u3000]*录")
VOL_HINT = re.compile(
    r"第\s*[一二三四五六七八九十百零\d]+\s*(?:套|年|卷|册)|精选卷|预测卷|模拟卷|真题卷"
)
VOL_NAME = re.compile(r"(第[一二三四五六七八九十百零\d]+[套年卷册]|真题精选卷|精选卷)")
NUM_PREFIX = re.compile(r"^\s*[（(]?\s*\d{1,2}\s*[)）.、]")
FOOT_CUT = 60          # 页脚带（pt）：拆出来的下半页连页码一起丢掉
TOP_ANCHOR = 40        # 下半页内容上移后，第一条正文落在这个高度
MIN_ABOVE_LINES = 3    # 标题上方至少这几行正文，才认定「这页挤了两卷」


def _cache_path(pdf: str, pages: int) -> Path:
    # 固定目录、按「文件名 + 页数」命名：不 mkdtemp、不删东西，重跑直接命中。
    key = re.sub(r"[^A-Za-z0-9]", "_", Path(pdf).stem)[:40]
    d = Path(tempfile.gettempdir()) / "pdf2wb_split"
    d.mkdir(exist_ok=True)
    return d / ("%s_%d.json" % (key, pages))


def _native_lines(pg) -> list[dict]:
    out = []
    for b in pg.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for l in b["lines"]:
            s = "".join(sp["text"] for sp in l["spans"]).strip()
            if s:
                out.append({"text": s, "box": list(l["bbox"])})
    return out


def line_records(doc, pdf: str, cache: Path) -> tuple[list[list[dict]], str]:
    """逐页拿 (文字, 坐标)。原生文字层直接用，扫描件跑整页 OCR 并缓存。"""
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            return data["pages"], data.get("source") or "缓存"
        except Exception:  # noqa: BLE001
            pass
    sample = [doc[i] for i in range(min(5, len(doc)))]
    chars = sum(len(p.get_text()) for p in sample) / max(1, len(sample))
    spans = []
    for p in sample[:2]:
        for b in p.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for l in b["lines"]:
                spans.extend(len(sp["text"].strip()) for sp in l["spans"])
    cps = sum(spans) / len(spans) if spans else 0.0
    # 后加的 OCR 层字很碎、坐标不可信，宁可重新识别一遍
    native = chars >= 120 and cps > 6
    source = "原书文字层" if native else "整页 OCR"
    pages = []
    for i, pg in enumerate(doc):
        lines = _native_lines(pg) if native else [
            {"text": l["text"], "box": l["box"]} for l in ocr.ocr_page(pg, dpi=200)
        ]
        pages.append(lines)
        if not native:
            print("  OCR p%d/%d：%d 行" % (i + 1, len(doc), len(lines)), flush=True)
    cache.write_text(json.dumps({"source": source, "pages": pages}, ensure_ascii=False),
                     encoding="utf-8")
    return pages, source


def find_toc(pages: list[list[dict]], max_pages: int = 6) -> tuple[int | None, list[str]]:
    """目录页：顶部有「目录」二字，且同页 ≥2 条像卷名的短行。"""
    for i, lines in enumerate(pages[:max_pages]):
        has_word = any(TOC_WORD.search(l["text"]) and l["box"][1] < 180 for l in lines)
        cands = [l["text"].strip() for l in lines
                 if VOL_HINT.search(l["text"]) and len(l["text"]) <= 50
                 and not NUM_PREFIX.match(l["text"])]
        if len(cands) >= 2 and (has_word or len(cands) >= 3):
            return i, cands
    return None, []


def short_names(titles: list[str]) -> list[str]:
    """去掉各卷共用的书名前缀，只留能区分卷次的那截。"""
    clean = [re.sub(r"\s+", "", t) for t in titles]
    prefix = os.path.commonprefix(clean)
    cut = max(prefix.rfind(ch) for ch in "·・/／、-：:（）()【】")
    if cut >= 4:
        names = [t[cut + 1:].strip() or t for t in clean]
    else:
        names = []
        for t in clean:
            m = VOL_NAME.search(t)
            names.append(t[m.start():] if m else t)
    # 去重保序
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def find_marks(pages, names, toc_idx, H: float):
    """正文里的卷标题：(页序, y0, y1, 卷名)。同一页只留最靠上的一条。"""
    marks: dict[int, tuple[float, float, str]] = {}
    for i, lines in enumerate(pages):
        if i == toc_idx:
            continue
        for l in lines:
            s = re.sub(r"\s+", "", l["text"])
            if not (6 <= len(s) <= 60) or NUM_PREFIX.match(s):
                continue
            hit = next((n for n in names if n in s), None)
            if not hit:
                continue
            y0, y1 = l["box"][1], l["box"][3]
            if i not in marks or y0 < marks[i][0]:
                marks[i] = (y0, y1, hit)
    return marks


def needs_split(lines, y0: float, H: float) -> bool:
    """标题落在页首附近且上方只有页眉 ⇒ 正常卷首页，不拆。"""
    if y0 <= 100 or y0 >= H * 0.8:
        return False
    above = [l for l in lines if l["box"][3] < y0 - 6]
    return len(above) >= MIN_ABOVE_LINES


def build_plan(doc, pages, marks, toc_idx, names):
    W, H = doc[0].rect.width, doc[0].rect.height
    plan: list[tuple[int, tuple, float]] = []      # (源页, clip, 上移量)
    starts: list[tuple[int, str]] = []             # (新页码, 卷名)
    splits = []
    for i in range(len(doc)):
        if i == toc_idx:
            continue
        m = marks.get(i)
        if m and needs_split(pages[i], m[0], H):
            y0, y1, name = m
            upper_end = max(y0 - 10, TOP_ANCHOR + 20)
            lower_start = min(y1 + 3, H - FOOT_CUT - 20)
            lower_end = H - FOOT_CUT
            shift = lower_start - TOP_ANCHOR
            plan.append((i, (0, 0, W, upper_end), 0.0))
            plan.append((i, (0, lower_start, W, lower_end), shift))
            starts.append((len(plan), name))       # 下半页才是这一卷的第一页
            splits.append({"source_page": i + 1, "volume": name,
                           "title_y": [round(y0, 1), round(y1, 1)],
                           "upper": [0, round(upper_end, 1)],
                           "lower": [round(lower_start, 1), round(lower_end, 1)],
                           "shift": round(shift, 1)})
        else:
            plan.append((i, (0, 0, W, H), 0.0))
            if m:
                starts.append((len(plan), m[2]))
    # 同一卷只认第一次出现，避免页眉/题干里的短名重复盖章
    seen, uniq = set(), []
    for pg, name in starts:
        if name not in seen:
            seen.add(name)
            uniq.append((pg, name))
    chapters = []
    for k, (pg, name) in enumerate(uniq):
        end = uniq[k + 1][0] - 1 if k + 1 < len(uniq) else len(plan)
        chapters.append({"title": name, "pages": "%d-%d" % (pg, max(pg, end))})
    return plan, chapters, splits, (W, H)


def write_pdf(doc, plan, size, out: Path) -> None:
    W, H = size
    dst = pymupdf.open()
    for src_i, clip, shift in plan:
        pg = dst.new_page(width=W, height=H)
        c = pymupdf.Rect(*clip)
        t = pymupdf.Rect(0, clip[1] - shift, W, clip[3] - shift)
        pg.show_pdf_page(t, doc, src_i, clip=c)
    dst.save(out, garbage=4, deflate=True)
    dst.close()


def patch_config(path: Path, chapters: list[dict]) -> None:
    import yaml
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if cfg.get("chapters"):
        print("  %s 里已有 chapters，不覆盖（%d 条）" % (path.name, len(cfg["chapters"])))
        return
    cfg["chapters"] = chapters
    # 行级读写交给 yaml 库，不用多行正则—— Windows 写出的 CRLF 会让 \n 正则静默失效
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print("  已把 %d 条 chapters 写进 %s" % (len(chapters), path))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="合订卷按卷拆页（输入侧预处理）")
    ap.add_argument("pdf")
    ap.add_argument("--out", help="拆分后的 PDF 路径，默认 <原名>_分册.pdf")
    ap.add_argument("--patch", help="把 chapters 写进这份 intake.yaml（已有则不覆盖）")
    ap.add_argument("--record", help="把拆页依据写到这份 split.json")
    ap.add_argument("--titles", help="目录是图片时手工给卷名，逗号分隔")
    a = ap.parse_args(argv)

    src = Path(a.pdf)
    out = Path(a.out) if a.out else src.with_name(src.stem + "_分册.pdf")
    doc = pymupdf.open(src)
    cache = _cache_path(str(src), len(doc))
    print("读文字：%s（%d 页）" % (src.name, len(doc)))
    pages, source = line_records(doc, str(src), cache)
    print("  来源：%s，缓存 %s" % (source, cache))

    toc_idx, titles = find_toc(pages)
    if a.titles:
        names = [t.strip() for t in a.titles.split(",") if t.strip()]
        why = "--titles 手工指定"
    elif titles:
        names = short_names(titles)
        why = "目录页 p%d 的 %d 条条目去公共前缀" % (toc_idx + 1, len(titles))
    else:
        print("\n没认出目录页，也没给 --titles。不猜卷名，停在这里。")
        print("用法：--titles '第一套试题,第二套试题,第三套试题,真题精选卷'")
        return 2
    print("  卷名（%d）：%s　[%s]" % (len(names), " / ".join(names), why))

    marks = find_marks(pages, names, toc_idx, doc[0].rect.height)
    plan, chapters, splits, size = build_plan(doc, pages, marks, toc_idx, names)
    if len(chapters) < 2:
        print("\n只找到 %d 卷，没必要拆页。原样退出。" % len(chapters))
        return 2

    write_pdf(doc, plan, size, out)
    record = {"source": str(src), "out": str(out), "source_pages": len(doc),
              "out_pages": len(plan), "dropped_pages": ([toc_idx + 1] if toc_idx is not None else []),
              "volume_names": names, "splits": splits, "chapters": chapters,
              "text_source": source}
    rec = Path(a.record) if a.record else out.with_name(out.stem + "_split.json")
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n拆页：%d 页 -> %d 页%s" % (len(doc), len(plan),
          "（丢掉目录页 p%d）" % (toc_idx + 1) if toc_idx is not None else ""))
    for s in splits:
        print("  原 p%d「%s」标题 y=%.1f：上半到 %.1f，下半 %.1f–%.1f 上移 %.1f"
              % (s["source_page"], s["volume"], s["title_y"][0], s["upper"][1],
                 s["lower"][0], s["lower"][1], s["shift"]))
    for c in chapters:
        print("  %s：%s" % (c["title"], c["pages"]))
    print("  新 PDF：%s" % out)
    print("  依据：%s" % rec)
    if a.patch:
        patch_config(Path(a.patch), chapters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
