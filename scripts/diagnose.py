"""诊断：selfcheck 报了问题，然后呢？

selfcheck 回答「哪里坏了」，本脚本回答「为什么坏、该动哪个参数、动完怎么验」。
这一步是把 SKILL.md 里那张「症状 -> 参数」表变成可执行的决策，而不是留给人去读。

为什么要分「系统性」和「局部」：
  同样是 5 处切穿，如果集中在同一页，那是页面级参数不对，改一行代码全书受益；
  如果散落在 20 页里，那是这 5 道题各自的版式特殊（大分式、分段函数大括号），
  改参数只会把另外 83 道好题弄坏。分不清这两者，就会陷入「调了参数更差」的循环。

用法：
  python selfcheck.py book.pdf out/ --json out/selfcheck.json
  python diagnose.py --check out/selfcheck.json --questions out/questions.json [--config config.yaml]

输出 out/diagnosis.md，并在终端打印结论。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# 处方表：每种症状对应「系统性」和「局部」两种解释，以及各自的动作。
# 这里的数值来自实际调参过程，改之前先看 why。
# ---------------------------------------------------------------------------
RULES = {
    "顶部切穿内容": {
        "meaning": "上边界压进了上一题末尾的公式，或把本题顶部削掉一条。",
        "systematic_when": "占比 > 8%，或同一页出现 2 处以上",
        "sys": {
            "file": "scripts/segment_ocr.py",
            "line": 153,
            "now": "if y - 30 <= g[1] <= y + 4 and (g[1] - g[0]) >= MIN_GAP_PT",
            "try": "把上界 y + 4 收紧到 y + 1，再跑一次",
            "why": ("上界留到 y+4 是为了容错 OCR 框顶和空白带只差 0.7pt 的情况（全书只有那一题需要）。"
                    "如果大面积切穿，说明这个容差在整本书上都在吃进上一题内容，收紧到 y+1。"),
        },
        "local": ("该题上方多半是大分式、积分号或分段函数的大括号，向上的笔画把空白带吃掉了一半。"
                  "不要改全局参数，给这一题在 questions.json 里单独下调 top（每次 2pt，改完只看这一题）。"),
    },
    "底部切穿内容": {
        "meaning": "下边界切进了下一题的题号或首行公式。",
        "systematic_when": "占比 > 8%，或同一页出现 2 处以上",
        "sys": {
            "file": "scripts/segment_ocr.py",
            "line": 136,
            "now": "MIN_GAP_PT = 4.0",
            "try": "降到 2.5，再跑一次",
            "why": ("题与题之间常常只隔 2~3pt 的白。4.0 这个门槛会让这类窄空白带被忽略，"
                    "吸附点于是跳到别处。降到 2.5 的代价是可能吸到题目内部的行间空隙，"
                    "所以要同时看「题高异常」的条数有没有上升。"),
        },
        "local": "同上，多为该题自身版式特殊，别动全局参数。",
    },
    "题高异常": {
        "meaning": "题目被压扁，说明切点掉进了题目内部的行间空隙。",
        "systematic_when": "占比 > 8%，或同一页出现 2 处以上",
        "sys": {
            "file": "scripts/segment_ocr.py",
            "line": 157,
            "now": "a, b = max(above, key=lambda t: t[1])",
            "try": "先确认不是「取最宽空白带」的策略又被改回来了；再检查 MIN_GAP_PT 是否被调高",
            "why": ("历史上出现过两次：一次是改成了「取最宽」，填空题一页 17 题被压成 17pt；"
                    "一次是门槛调到 6pt，把题间的窄白全滤掉了。这条症状几乎总是策略写错，"
                    "而不是参数微调能解决的。"),
        },
        "local": "单题压扁多半是页脚（如 ·1·）被并进了题目区间，检查 ocr.content_bottom_margin。",
    },
}

# 题数与页数的经验区间：一页习题册通常放 2~12 题
QPS_MIN, QPS_MAX = 0.3, 12.0


def load_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def group(problems):
    g = defaultdict(list)
    for p in problems:
        g[p["kind"]].append(p)
    return g


def is_systematic(items, total_q):
    """判定标准三条，满足其一即可。

    关键是第二条「同页聚集」：连续两处切穿在同一页，几乎一定是页面级偏移，
    而不是两道题各自版式特殊。反过来，散落在整本书里的三五处，改全局参数
    只会把没问题的题弄坏。
    """
    if total_q and len(items) / max(total_q, 1) > 0.08:
        return True, f"占全部 {total_q} 题的 {len(items) / total_q:.1%}，已不是个例"
    by_page = Counter(p["page"] for p in items)
    if by_page and max(by_page.values()) >= 2:
        top_page, top_cnt = by_page.most_common(1)[0]
        return True, f"第 {top_page + 1} 页就有 {top_cnt} 处，属于页面级偏移"
    if len(items) >= 10:
        return True, f"同类问题 {len(items)} 处，即使散落也说明存在共性原因"
    return False, "零散分布且占比低，缺少共同特征，更像个别题目版式特殊"


def diagnose(check, questions, cfg=None):
    probs = check["problems"]
    total_q = check["questions"]
    lines = ["# 切点诊断", ""]

    if not probs:
        lines += ["自检通过，无需诊断。", ""]
        return "\n".join(lines)

    lines += [
        f"- 题目总数：{total_q}",
        f"- 问题条数：{len(probs)}（{len({p['seq'] for p in probs})} 道题受影响）",
        f"- 原书页数：{check['pages']}",
        "",
    ]

    # 先做一个整体合理性检查：题数少得离谱时，问题不在切点而在识别
    qps = total_q / max(check["pages"], 1)
    if qps < QPS_MIN:
        lines += [
            "## 先看这个：题数可能整体漏了",
            "",
            f"平均每页只有 {qps:.2f} 题，低于经验下限 {QPS_MIN}。",
            "切点问题修得再好也救不回来——先去 probe.md 看题号正则命中率，",
            "很可能是正则没匹配上这种排版，或者扫描件的左栏条带 OCR 没识别出题号。",
            "",
        ]

    g = group(probs)
    plan, manual = [], []

    for kind, items in sorted(g.items(), key=lambda kv: -len(kv[1])):
        rule = RULES.get(kind)
        sys_flag, reason = is_systematic(items, total_q)
        ids = "、".join(x["id"] for x in items[:8]) + ("…" if len(items) > 8 else "")
        lines += [f"## {kind}（{len(items)} 处）", ""]
        lines.append(f"- 涉及：{ids}")
        lines.append(f"- 判定：{'**系统性**' if sys_flag else '局部'} —— {reason}")
        if rule:
            lines.append(f"- 含义：{rule['meaning']}")
        lines.append("")

        if sys_flag and rule:
            s = rule["sys"]
            lines += [
                f"**动作**：改 `{s['file']}` 第 {s['line']} 行附近",
                "",
                "```",
                f"当前：{s['now']}",
                f"试改：{s['try']}",
                "```",
                "",
                f"依据：{s['why']}",
                "",
            ]
            plan.append((kind, s["file"], s["line"], s["try"]))
        elif rule:
            lines += [f"**动作**：不改全局参数。{rule['local']}", ""]
            manual.extend(items)
        else:
            lines += ["**动作**：未收录的症状，按上面的「含义」人工看几道再决定。", ""]
            manual.extend(items)

    # 处方清单
    if plan:
        lines += ["## 改动清单（按此顺序，改一条验一次）", ""]
        for i, (kind, f, ln, act) in enumerate(plan, 1):
            lines.append(f"{i}. [{kind}] `{f}:{ln}` —— {act}")
        lines += [
            "",
            "每次只改一条，改完立刻重跑：",
            "",
            "```",
            "python segment_ocr.py book.pdf out/ config.yaml   # 或 segment.py / photo_pipeline.py",
            "python selfcheck.py book.pdf out/ --json out/selfcheck.json",
            "python diagnose.py --check out/selfcheck.json --questions out/questions.json",
            "```",
            "",
            "**如果改完某一类问题少了、另一类变多，说明两条判据在互相拉扯**——",
            "这时候回退这一条，把它归到人工清单，不要继续加码。",
            "",
        ]

    if manual:
        lines += ["## 人工清单（改参数救不了的）", ""]
        for p in manual[:40]:
            lines.append(f"- `{p['id']}` 原书 p{p['source_page']} {p['kind']} {p['where']}")
        if len(manual) > 40:
            lines.append(f"- …共 {len(manual)} 处")
        lines += ["", "这些题版式特殊，逐题看图确认，或在 questions.json 里单独调这一题的 rect。", ""]

    return "\n".join(lines)


def main(argv):
    ap = argparse.ArgumentParser(description="切点问题诊断")
    ap.add_argument("--check", required=True, help="selfcheck --json 产出的 json")
    ap.add_argument("--questions", required=True, help="questions.json")
    ap.add_argument("--config", default=None)
    ap.add_argument("-o", "--out", default=None, help="诊断报告路径，默认放在 check 同级 diagnosis.md")
    a = ap.parse_args(argv)

    check = load_json(a.check)
    load_json(a.questions)  # 只为早失败：文件坏了在这里就报
    md = diagnose(check, a.questions)

    out = Path(a.out) if a.out else Path(a.check).parent / "diagnosis.md"
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
