"""结构校验：题号连不连续、有没有跳号重号、跨页合并对不对。

切点切歪只坏一道题；题号断了则坏整册——书签、页码、按题号对答案全部错位。
而且这种错在缩略图墙上**完全看不出来**：每道题本身都是完整、清晰、没有残渣的，
只有把题号排成一列才看得见。

两种用法：
  python structural.py out/questions.json --json out/structural.json
  from structural import check; check(items)

输入只要求 items 是「像 questions.json 那样的列表」，因此 reflow 的 items.json
也能直接复用（字段不全时相关检查自动跳过）。

退出码：0 通过，1 有高危问题。build.py 不靠它中止，只把它转成校对单的排序信号。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

NUM = re.compile(r"\d+")

HIGH, MID, LOW = "high", "mid", "low"

# 页首残段（上一题跨页的尾巴）本来就没有题号，不能当成编号序列的一员
_NO_NUMBER_HINTS = ("页首", "残段", "续页")


def _nums(qid: str) -> list[int] | None:
    """从题号里取出数字层级：'4. （3）' -> [4,3]，'选12' -> [12]，'18.4' -> [18,4]。"""
    if any(h in qid for h in _NO_NUMBER_HINTS):
        return None
    found = NUM.findall(qid)
    return [int(x) for x in found] if found else None


def _rng(vals) -> str:
    """把缺失的编号压成可读区间：11-13, 15。"""
    vals = list(vals)
    if not vals:
        return ""
    out, start, prev = [], vals[0], vals[0]
    for v in vals[1:]:
        if v == prev + 1:
            prev = v
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = v
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(out)


def _page_of(q: dict) -> int:
    if q.get("source_page"):
        return q["source_page"]
    p = (q.get("parts") or [{}])[0].get("page", 0)
    return p + 1


def _problem(q: dict, kind: str, severity: str, detail: str) -> dict:
    return {
        "seq": q.get("seq"),
        "id": str(q.get("id", "")),
        "source_page": _page_of(q),
        "kind": kind,
        "severity": severity,
        "detail": detail,
    }


def check(items: list[dict], pages: int | None = None, geometry: bool = True) -> dict:
    """geometry=False 用于 reflow 的 items.json —— 那里没有 parts/rect，
    跑几何检查只会把每一题都报成「题区间为空」。"""
    problems: list[dict] = []

    # ---- 1. 序号连续（seq 是排版顺序，和题号是两回事，但错位说明合并逻辑动了）----
    # 只报第一处断点：删掉一题会让后面每一项的 seq 都差 1，逐项报会淹没真正的原因。
    bad = [(i, q) for i, q in enumerate(items, 1) if q.get("seq") and q["seq"] != i]
    if bad:
        i, q = bad[0]
        problems.append(
            _problem(q, "序号不连续", MID, f"seq={q['seq']}，按位置应为 {i}；其后共 {len(bad)} 处错位")
        )

    # ---- 2. 几何有效性（裁不出东西的题，后面所有检查都没意义）----
    for q in (items if geometry else []):
        parts = q.get("parts") or []
        if not parts:
            problems.append(_problem(q, "题区间为空", HIGH, "parts 为空，裁不出内容"))
            continue
        for p in parts:
            rect = p.get("rect") or [0, 0, 0, 0]
            if rect[3] - rect[1] < 6:
                problems.append(
                    _problem(q, "题区间过窄", HIGH, f"p{p.get('page', 0) + 1} 高仅 {rect[3] - rect[1]:.1f}pt")
                )
        if q.get("span_pages") and len(parts) != q["span_pages"]:
            problems.append(
                _problem(q, "跨页计数不符", MID, f"span_pages={q['span_pages']}，实际 parts={len(parts)}")
            )

    # ---- 3. 编号连续性：按「章 + 题型」分组，章节内各自从 1 数 ----
    # 只按题型分组是不够的：真题分类册里每个命题点都从「选1」重新编号，
    # 全书会有十几个「选21」。拿它当重号，一本 703 题的书能报出 77 处高危，
    # 真正的高危就被淹了。有 chapter 就按章切，没有（文本层老题库）退回按题型。
    groups: dict[tuple, list[dict]] = OrderedDict()
    for q in items:
        key = (q.get("part") or "", q.get("chapter") or "", q.get("section") or "")
        groups.setdefault(key, []).append(q)

    for (part, chap, sec), qs in groups.items():
        numbered = [(q, _nums(str(q.get("id", "")))) for q in qs]
        numbered = [(q, n) for q, n in numbered if n]
        if not numbered:
            continue
        label = (chap + " " + sec).strip() if chap else (sec or "正文")

        # 重号：按**完整编号**判，不是按末级。18.3 和 18.4 同属第 18 章，
        # 按章号判会把整章 9 道题全报成重号。
        by_key: dict[tuple, list[dict]] = OrderedDict()
        for q, n in numbered:
            by_key.setdefault(tuple(n), []).append(q)
        for key, dup in by_key.items():
            if len(dup) > 1:
                name = ".".join(str(x) for x in key)
                pages = "、".join(f"p{_page_of(q)}" for q in dup)
                problems.append(
                    _problem(dup[-1], "题号重复", HIGH, f"{label} 第 {name} 题出现 {len(dup)} 次（{pages}）")
                )

        by_major: dict[int, list[tuple[dict, list[int]]]] = OrderedDict()
        for q, n in numbered:
            by_major.setdefault(n[0], []).append((q, n))

        # 章号跳号 / 乱序
        majors = list(by_major)
        for a, b in zip(majors, majors[1:]):
            if b == a + 1:
                continue
            if b > a + 1:
                miss = list(range(a + 1, b))
                problems.append(
                    _problem(
                        by_major[b][0][0],
                        "跳号",
                        HIGH,
                        f"{label} 第 {a} 题之后直接到第 {b} 题，缺 {len(miss)} 题：{_rng(miss)}",
                    )
                )
            else:
                problems.append(
                    _problem(by_major[b][0][0], "题号乱序", HIGH, f"{label} 第 {a} 题之后出现第 {b} 题")
                )

        # 末级编号（18.3 → 18.11 里的 3 和 11）。整册都写成两级时这是主编号，
        # 缺一个就少一题，判高危；混用时（如 4. 与 4.(3) 并存）只作提醒。
        hierarchical = all(len(n) >= 2 for _, n in numbered)
        for m, lst in by_major.items():
            subs = sorted({n[1] for _, n in lst if len(n) > 1})
            if len(subs) < 2:
                continue
            missing = [x for x in range(subs[0], subs[-1] + 1) if x not in set(subs)]
            if not missing:
                continue
            # 指向缺号之后的第一个题（和「跳号」报在到达点同一个习惯）
            after = [(q, n) for q, n in lst if len(n) > 1 and n[1] > missing[0]]
            first_q = after[0][0] if after else lst[0][0]
            problems.append(
                _problem(
                    first_q,
                    "子题号不连续",
                    HIGH if hierarchical else LOW,
                    f"{label} 子题缺 {_rng(missing)}"
                    + ("" if hierarchical else "（整章只做部分子题时可忽略）"),
                )
            )

    # ---- 4. 页首残段独立成题：说明上一页末尾没识别到题号 ----
    for q in items:
        if any(h in str(q.get("id", "")) for h in _NO_NUMBER_HINTS):
            problems.append(
                _problem(q, "页首残段独立成题", MID, "上一页末尾没识别到题号，这段内容也许该并进上一题")
            )

    # ---- 5. 章节摘要，给人一眼核对范围 ----
    # 分组键是 (部分, 命题点, 题型) 元组，摘要里要摊成可读字符串——
    # 直接塞元组会让 build.py 的 json.dumps 抛 TypeError（key 必须是 str）。
    summary = OrderedDict()
    for key, qs in groups.items():
        if isinstance(key, tuple):
            part, chap, sec = key
            label = " ".join(x for x in (chap, sec) if x) or part or "正文"
        else:
            label = key or "正文"
        pgs = [_page_of(q) for q in qs]
        summary[label] = {
            "count": len(qs),
            "first": str(qs[0].get("id", "")),
            "last": str(qs[-1].get("id", "")),
            "page_from": min(pgs),
            "page_to": max(pgs),
        }

    # 高危排最前：报告一开头看到的就是「必须处理」的那几条
    order = {HIGH: 0, MID: 1, LOW: 2}
    problems.sort(key=lambda p: (order[p["severity"]], p["seq"] or 0))

    return {
        "questions": len(items),
        "pages": pages,
        "problems": problems,
        "by_severity": dict(Counter(p["severity"] for p in problems)),
        "sections": summary,
    }


def render(r: dict) -> str:
    lines = ["## 结构校验", ""]
    for sec, s in r["sections"].items():
        lines.append(
            f"- {sec}：{s['count']} 题，{s['first']} → {s['last']}，p{s['page_from']}–p{s['page_to']}"
        )
    lines.append("")

    high = [p for p in r["problems"] if p["severity"] == HIGH]
    mid = [p for p in r["problems"] if p["severity"] == MID]
    low = [p for p in r["problems"] if p["severity"] == LOW]

    if not r["problems"]:
        lines += [f"结构校验通过：{r['questions']} 题，题号连续、无重号。", ""]
        return "\n".join(lines)

    lines += [f"发现 {len(r['problems'])} 处：{len(high)} 高危 / {len(mid)} 需确认 / {len(low)} 提醒", ""]
    for tag, bucket in (("高危", high), ("需确认", mid), ("提醒", low)):
        if not bucket:
            continue
        lines.append(f"### {tag}")
        lines.append("")
        for p in bucket:
            lines.append(f"- `{p['id']}` p{p['source_page']} {p['kind']} —— {p['detail']}")
        lines.append("")

    if high:
        lines += [
            "高危里有任何一条，先别交付：题号错了，做题本按题号对答案、"
            "书签跳转全部会指到别处，而每道题看起来都是好的。",
            "",
        ]
    return "\n".join(lines)


def load_questions(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):          # reflow 的 items.json
        items = data.get("items", [])
        for i, it in enumerate(items):
            it.setdefault("seq", i + 1)
            it.setdefault("id", str(it.get("no", "")))
        return items
    return data


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="题号结构校验")
    ap.add_argument("questions", help="questions.json 或 reflow 的 items.json")
    ap.add_argument("--json", dest="json_path", default=None)
    ap.add_argument("--pages", type=int, default=None, help="原书页数，仅用于报告")
    a = ap.parse_args(argv)

    items = load_questions(a.questions)
    r = check(items, a.pages)
    if a.json_path:
        Path(a.json_path).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")

    text = render(r)
    print(text)
    if a.json_path:
        print(f"-> {a.json_path}")
    return 1 if any(p["severity"] == HIGH for p in r["problems"]) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
