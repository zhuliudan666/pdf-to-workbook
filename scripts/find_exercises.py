"""讲义里只切课后题：先圈出每章「习题」小节在哪，再交给切题流水线。

## 为什么需要单独一步

真题卷和题库是「整本都是题」，`segment.py` 从头扫到尾就行。讲义不是：
正文占九成，题只在每章末尾那一两页。直接跑的结果是把「定理 1.1」「例 1.2」
「公式 (1.3)」全当成题号切下来——切出几十道「定理」，而且看起来都像正常的题。

所以先圈范围。这本书的骨架是：

    第 3 章 中值定理                       ← 章标题
      3.1 基本概念                        ← 节标题（正文，跳过）
      ...正文...
      习题 3.1                            ← 从这里开始
      1. ... 2. ... 3. ...                ← 这些才是要的
    第 4 章 ...                           ← 到这里结束

输出是 `common.parse_pages` 吃得下的多段页码 `"12-14,58-61"`，后面所有步骤不用改。

## 一句实话

小标题不统一就一定漏：有的章叫「习题」，有的叫「练习」，有的干脆不写标题、
直接接着正文排题。所以这里**宁可少圈，也不猜**——圈不出来的章在报表里标「未找到」，
让人翻一眼目录补页码，比默默切错几十道题便宜。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import common

HERE = Path(__file__).resolve().parent.parent

# 「习题小节」的标题。这类词在正文里极少出现在行首，误判风险低。
EX_RE = re.compile(r"^\s*(?:课后习题|习题|练习|练习题|思考题|Exercises|Problems)\s*[\d.．\-]*\s*$", re.I)
# 章级标题。用来判定「这一章到哪结束了」，也是报表里的章名来源。
CH_RE = re.compile(r"^\s*(第\s*[一二三四五六七八九十百零〇\d]+\s*[章讲]|Chapter\s*\d+)\b", re.I)


def find_sections(doc) -> list[dict]:
    """扫全书，返回每段课后题的 (章, 标题, 起止页)。

    判定「结束」的规则：下一个「习题」标题或下一个「章」标题，谁先到算谁。
    为什么不是「下一个同级标题」：讲义的小节层级不统一，3.1/3.2 这种也可能是
    正文小节而不是习题，按层级判会把习题段切断在无关的地方。
    """
    heads: list[tuple[int, float, str, str]] = []   # (page, y, kind, text)
    for pno, page in enumerate(doc):
        for ln in common.page_lines(page):
            t = ln["text"].strip()
            if not t:
                continue
            if EX_RE.match(t):
                heads.append((pno, ln["y0"], "ex", t[:40]))
            elif CH_RE.match(t):
                heads.append((pno, ln["y0"], "ch", t[:40]))
    heads.sort(key=lambda h: (h[0], h[1]))

    out: list[dict] = []
    chapter = ""
    for i, (pno, y, kind, text) in enumerate(heads):
        if kind == "ch":
            chapter = text
            continue
        # 找这一段题的终点。
        # 下一段是「习题」标题：那一段新题从 p2 开始，本题段最多吃到 p2 这一页
        #   （同一页半页旧题、半页新题，页级切分只能整页给它，后面切点自己会分开）。
        # 下一段是「章」标题：这一章的题到章标题**之前**就结束了。p2 是下一章的正文页，
        #   把它算进来就会整页吃正文——第 1 章习题在 p3、第 2 章标题在 p4，圈成 p3-4
        #   的效果是 p4 上的「定理 2.1」全被当题切下来。这是这本讲义第一版的实际结果。
        end_page = len(doc) - 1
        for p2, y2, k2, _ in heads[i + 1:]:
            if p2 == pno and y2 <= y:
                continue
            if k2 == "ch":
                end_page = p2 - 1 if p2 > pno else pno
            else:
                end_page = p2
            break
        out.append({"chapter": chapter, "title": text,
                    "start_page": pno + 1, "end_page": end_page + 1,
                    "start_y": round(y, 1)})
    return out


def count_questions(doc, sections: list[dict], cfg: dict) -> None:
    """给每段数一下题号，填料进 sections（就地改）。

    数出来是 0 的两段要当心：要么这一段确实没题（只有标题），
    要么题目编号跟这本的 question_patterns 不匹配——后者才是要查的。
    """
    for s in sections:
        n = 0
        for p in range(s["start_page"] - 1, min(s["end_page"], len(doc))):
            page = doc[p]
            lines = common.content_lines(page, cfg)
            best = 0
            for pat in cfg["question_patterns"]:
                best = max(best, len(common.find_marks(lines, {**cfg, "question_patterns": [pat]})))
            n += best
        s["questions"] = n


def to_pages_spec(sections: list[dict]) -> str:
    """合成 "1-2,5-6" 这种多段页码，直接喂 common.parse_pages。"""
    chunks = [f"{s['start_page']}-{s['end_page']}" for s in sections]
    # 相邻段重合时合并，避免同一页被切两次
    merged: list[list[int]] = []
    for c in chunks:
        lo, _, hi = c.partition("-")
        lo, hi = int(lo), int(hi or lo)
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return ",".join(f"{a}-{b}" for a, b in merged)


def to_chapters_yaml(sections: list[dict]) -> str:
    """生成直接贴进 config.yaml 的 chapters 段。

    章名后面必须跟 pages，光有章名 segment 不知道怎么盖章；
    这里按 find_sections 算出来的页段填，用户不用手抄。
    """
    L = ["# 章 → 页段。segment 按它给每题盖 chapter，结构校验才不会把每章的「第 1 题」当重号。",
         "chapters:"]
    for s in sections:
        title = (s["chapter"] or s["title"] or "").strip()
        rng = f"{s['start_page']}-{s['end_page']}"
        L.append(f"  - title: {json.dumps(title, ensure_ascii=False)}")
        L.append(f"    pages: {json.dumps(rng)}")
    return "\n".join(L) if len(L) > 2 else ""


def patch_config(cfg_path: str, block: str, pages_spec: str = "") -> str:
    """把 pages / chapters 追加到配置末尾。

    用文本追加而不是 yaml.safe_load + dump：配置里全是逐条解释「为什么这么定」的注释，
    过一遍 yaml 往返会把它们全丢掉——那些注释是这份配置最值钱的部分。
    已有同名键就不动，让人自己删旧的，免得同一个键出现两次。

    pages 只在配置里还没有时才写：用户说过「只做 60-75 页」时，
    圈出来的页段是交集还是替换，得他说了算，不能替他决定。
    """
    p = Path(cfg_path)
    text = p.read_text(encoding="utf-8")
    notes = []
    add = []
    if pages_spec:
        # intake 在「全书」时会写一行 `pages: null`（yaml 把 None 落成 null）。
        # 那等于没设，直接改掉；只有真的填了页码才不覆盖——那种情况是用户
        # 自己说过范围，跟圈出来的页段该不该取交集得他决定。
        empty = re.search(r"^\s*pages\s*:\s*(?:null|~|)\s*$", text, re.M)
        if empty:
            text = text[:empty.start()] + f"pages: {json.dumps(pages_spec)}" + text[empty.end():]
        elif re.search(r"^\s*pages\s*:", text, re.M):
            notes.append("配置里已经有 pages:（你指定过范围），没覆盖——"
                         "要改成圈出来的页段，把旧的那行删掉或直接改成上面的页码。")
        else:
            add.append(f"pages: {json.dumps(pages_spec)}")
    if re.search(r"^\s*chapters\s*:", text, re.M):
        notes.append("配置里已经有 chapters:，没动。要换页段先把旧的那段删掉。")
    elif block:
        add.append(block)
    if not add:
        return "\n".join(notes) if notes else "没有要写的。"
    p.write_text(text.rstrip("\n") + "\n\n" + "\n".join(add) + "\n", encoding="utf-8")
    return "\n".join([f"已写入 {p}（追加在文件末尾）"] + notes)


def render(src: str, sections: list[dict], spec: str) -> str:
    total = sum(s["questions"] for s in sections)
    L = [f"# 课后题范围：{Path(src).name}", "",
         f"找到 {len(sections)} 段课后题，共约 {total} 道。", ""]
    if not sections:
        return "\n".join(L + ["一段都没找到。检查标题写法：这本书的习题小节可能不叫「习题/练习」，",
                              "或者压根没写标题（直接接着正文排题）。这种情况只能翻目录给页码范围。"])
    L += ["| 章 | 小节 | 页 | 题数 |", "| --- | --- | --- | --- |"]
    for s in sections:
        L.append(f"| {s['chapter'] or '—'} | {s['title']} | p{s['start_page']}–{s['end_page']} | {s['questions']} |")
    L += ["", "页码范围（直接填进 config.yaml 的 pages）：", "", f"    pages: {spec}", ""]
    chap = to_chapters_yaml(sections)
    if chap:
        L += ["章名映射（--patch 配置 会直接追加，也可以手动贴）：", ""]
        L += ["    " + ln for ln in chap.splitlines()]
        L.append("")
    zero = [s for s in sections if s["questions"] == 0]
    if zero:
        L += ["", f"> ⚠ {len(zero)} 段数不出题号：{'、'.join(s['title'] for s in zero[:5])}。"
              "要么这段确实只有标题，要么题号正则不匹配这本——先看 probe.md 的命中表。"]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="圈出讲义里每章的课后题页段")
    ap.add_argument("src")
    ap.add_argument("--config", default="")
    ap.add_argument("--patch", default="", help="把 chapters 段追加到这个配置文件")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)

    cfg = common.load_config(a.config or str(HERE / "config.yaml"))
    doc = pymupdf.open(a.src)
    sections = find_sections(doc)
    count_questions(doc, sections, cfg)
    spec = to_pages_spec(sections)
    doc.close()

    print(render(a.src, sections, spec))
    if a.patch:
        block = to_chapters_yaml(sections)
        if block:
            print(patch_config(a.patch, block, spec))
        else:
            print("一段都没找到，没东西可写")
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps({"sections": sections, "pages": spec},
                                           ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n-> {a.json}")
    return 0 if sections else 1


if __name__ == "__main__":
    raise SystemExit(main())
