"""期望题数：从原书的题型标题推出「每卷几题 × 几卷」，跟实际切出的题数对账。

为什么需要：结构校验只问「题号连不连得上」，不问「这本书应该有多少题」。
一本 4 套 × 22 题的合订卷只切出 40 题时，结构校验报几处跳号，
accept 的六条闸却全是绿的——每一张题图单看都完整，只有把「应该有多少题」
摆出来才知道少了一半。这条闸补的就是那个数。

判据全部从文件本身来，认不出就如实报「未覆盖」，不猜：
  1. chapters 里 ≥2 条命中「第N套/年/卷/册」⇒ 这是分册合订，卷数 = chapters 条数
     （不要求每条都带「第N」——「真题精选卷」这类第 4 卷也算一卷）
  2. 原书文字层 / 整页 OCR 缓存里，题型标题行的「N～M 小题」取最大上界 M
     ⇒ 每卷应有 M 题；期望 = M × 卷数
  3. 指定了 pages（只做一部分）时不推断，免得拿整册的数去卡半本
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pymupdf

MULTI_VOL = re.compile(r"第\s*[一二三四五六七八九十百零\d]+\s*(?:套|年|卷|册)")
RANGE_RE = re.compile(
    r"(?:选择题|填空题|解答题|证明题)[^\d]{0,16}?(\d{1,2})\s*[～~〜\-—至]\s*(\d{1,2})\s*(?:小题|题)"
)


def _lines_native(pdf: str) -> list[str]:
    doc = pymupdf.open(pdf)
    try:
        return [ln for pg in doc for ln in pg.get_text("text").splitlines()]
    finally:
        doc.close()


def _lines_ocr(out: Path) -> list[str]:
    lines: list[str] = []
    for f in sorted((out / "ocr").glob("page_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 单个缓存坏了不该让整条闸崩掉
            continue
        lines.extend(str(l.get("text") or "") for l in data.get("lines") or [])
    return lines


def collect_lines(pdf: str | None, out: Path, text_layer: str) -> tuple[list[str], str]:
    """优先用原书原生文字层；扫描件用 crop 阶段落下的整页 OCR 缓存。"""
    if pdf and Path(pdf).exists() and text_layer == "ok":
        return _lines_native(pdf), "原书文字层"
    lines = _lines_ocr(out)
    if lines:
        return lines, "整页 OCR 缓存（%d 行）" % len(lines)
    return [], "没有可用的文字来源"


def scan(pdf: str | None, out: Path, cfg: dict, text_layer: str = "auto") -> dict:
    """返回期望题数规格。covered=False 时 reason 写明为什么没查，不许静默略过。"""
    spec = {"covered": False, "per_volume": None, "volumes": None,
            "expected": None, "evidence": "", "reason": ""}
    if cfg.get("pages"):
        spec["reason"] = "只做了指定页段，不能拿整册的应有题数对账"
        return spec
    chaps = cfg.get("chapters") or []
    vol_hits = sum(1 for c in chaps if MULTI_VOL.search(str(c.get("title") or "")))
    if len(chaps) < 2 or vol_hits < 2:
        spec["reason"] = ("chapters 不是多套合订（%d 条，命中分册判据 %d 条），不推断应有题数"
                          % (len(chaps), vol_hits))
        return spec
    lines, src = collect_lines(pdf, out, text_layer)
    if not lines:
        spec["reason"] = "没有可用的题型标题文字（%s）" % src
        return spec
    ups = [int(m.group(2)) for s in lines for m in RANGE_RE.finditer(s)]
    if not ups:
        spec["reason"] = "题型标题里没读到「N～M 小题」这类区间（%s）" % src
        return spec
    per, vol = max(ups), len(chaps)
    spec.update(covered=True, per_volume=per, volumes=vol, expected=per * vol,
                evidence="每卷最大题号 %d（%s）× %d 个分册" % (per, src, vol))
    return spec


def check(actual: int, spec: dict) -> tuple[dict | None, str | None, str]:
    """(硬伤, 未覆盖警告, 正常回执)。三选一，绝不悄悄通过。"""
    if not spec.get("covered"):
        return None, "期望题数未覆盖：%s" % (spec.get("reason") or "原因不明"), ""
    exp = int(spec["expected"])
    if actual != exp:
        return ({"seq": None, "id": "", "source_page": 0, "kind": "question_count",
                 "severity": "high",
                 "detail": "应有 %d 题（%s），实际切出 %d 题" % (exp, spec["evidence"], actual)},
                None, "")
    return None, None, "题数对账：%d / %d，一致（%s）" % (actual, exp, spec["evidence"])
