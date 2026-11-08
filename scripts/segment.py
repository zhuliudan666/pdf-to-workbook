"""切题：按题号正则把 PDF 分成一道一道题，输出 questions.json。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).parent))
import common


def main(pdf: str, cfg_path: str | None = None, out_dir: str | None = None) -> list[dict]:
    cfg = common.load_config(cfg_path)
    doc = pymupdf.open(pdf)
    questions = common.build_questions(doc, cfg)
    # 讲义/多章练习册要按章分组（结构校验判重号、版面画章标题都靠它）。
    # 章名不写在正则里，写在配置的 chapters 里——书跟书的分章规则没法统一。
    stamped = common.stamp_chapters(questions, cfg)

    out = Path(out_dir or Path(pdf).parent / "workbook_out")
    out.mkdir(parents=True, exist_ok=True)
    payload = [
        {k: q.get(k) for k in ("id", "seq", "source_page", "span_pages",
                               "section", "chapter", "parts")}
        for q in questions
    ]
    (out / "questions.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    merged = sum(1 for q in questions if q["span_pages"] > 1)
    print(f"切出 {len(questions)} 题，其中跨页合并 {merged} 题 -> {out / 'questions.json'}")
    if stamped:
        print(f"按 chapters 盖上章名：{stamped} 题（其余 {len(questions) - stamped} 题不在任何章段里）")
    print("题号序列：" + " ".join(q["id"] for q in questions[:30]))
    return payload


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python segment.py <pdf> [config.yaml] [输出目录]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, sys.argv[3] if len(sys.argv) > 3 else None)
