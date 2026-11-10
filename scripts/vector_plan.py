"""给「矢量重排」生成转写模板：把位图流水线裁出的题图变成 items.json + 任务说明。

脚本不做转写——那一步要同时看懂中文和二维公式，只能由多模态模型（agent）逐题完成。
这里负责把「哪些题、每张图在哪、题号是什么、按什么规则写」固定下来，
让 agent 接手时不用再猜，也让 uncertain 有地方落。

用法：
  python vector_plan.py 输出目录                  # PDF 路线：读 questions.json
  python vector_plan.py 照片_work --images "q_*.png"   # 照片路线：读裁片
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

KIND = {"选择题": "choice", "填空题": "fill", "解答题": "solve", "证明题": "solve"}
TASK = """# 矢量重排 · 转写任务

这本选了「矢量重排」。脚本只把模板准备好，**转写由 agent 逐题完成**：

1. 打开 `items.json`，共 {n} 条，每条的 `src_image` 指向该题的原图裁片。
2. 逐条处理：
   - 读那张裁片（多模态看图），把题干写进 `stem`：中文与行内公式混排，公式用 `$...$` 包住。
   - 独立成行的居中公式写 `disp`；小问写 `subs`；选择题的 (A)(B)(C)(D) 按原书顺序写全。
   - `kind` 按实际题型改成 `choice` / `fill` / `solve`，`no` 保持切题时的题号。
   - **任何拿不准的符号都写进 `uncertain`，一句一条。** 宁可写「看不清，可能是 $e^x$」，
     也不要猜一个看起来合理的值——这条路唯一的失败模式就是「认错了但看起来完全正常」。
3. 转写提示词见 `references/transcribe-prompt.md`；`uncertain` 是必填项，不写等于没说过。
4. 全部填完再跑排版：
   ```bash
   python scripts/reflow.py "{items}" -o "{pdf}"
   ```
   `reflow.py` 发现 `src_image` / `uncertain` 会自动出一份校对单（上原图、下转写）。
5. 打开校对单，红框逐题对照原图核。这一遍省不掉。

认门回执里给的是粗估时间；实际题数以 `items.json` 的条数为准。
"""


def _title(out: Path) -> str:
    p = out / "intake.yaml"
    if p.exists():
        try:
            import yaml
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            h = cfg.get("header") or {}
            t = str(h.get("title") or h.get("book") or "").strip()
            if t:
                return t
        except Exception:  # noqa: BLE001
            pass
    return out.name


def _rel(path: str, out: Path) -> str:
    """题图路径尽量写成相对 items.json 的，方便整个目录搬走。"""
    try:
        r = os.path.relpath(path, out).replace(os.sep, "/")
        return r if not r.startswith("..") else path
    except ValueError:      # 跨盘符
        return path


def collect(out: Path, images: str | None) -> tuple[list[dict], str]:
    qf = out / "questions.json"
    if qf.exists() and not images:
        qs = json.loads(qf.read_text(encoding="utf-8"))
        items = []
        for q in qs:
            items.append({
                "no": str(q.get("id") or ""),
                "kind": KIND.get(q.get("section") or "", "solve"),
                "stem": "", "disp": "", "subs": [],
                "src_image": _rel(str(q.get("image") or ""), out),
                "uncertain": [],
            })
        return items, "questions.json"

    cf = out / "crops.json"
    if cf.exists() and not images:
        rows = json.loads(cf.read_text(encoding="utf-8"))
        return ([{
            "no": str(r.get("no") or i + 1),
            "kind": str(r.get("kind") or "solve"),
            "stem": "", "disp": "", "subs": [],
            "src_image": _rel(str(r.get("image") or ""), out),
            "uncertain": [],
        } for i, r in enumerate(rows)], "crops.json")

    files = sorted(glob.glob(images) if images and os.path.isabs(images)
                   else [str(p) for p in out.glob(images or "q_*.png")])
    return ([{
        "no": str(i + 1),
        "kind": "solve",
        "stem": "", "disp": "", "subs": [],
        "src_image": _rel(f, out),
        "uncertain": [],
    } for i, f in enumerate(files)], "裁片文件")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="生成矢量重排的 items.json 模板")
    ap.add_argument("dir", help="输出目录（PDF 路线）或照片 --work 目录")
    ap.add_argument("--images", help="照片路线的裁片 glob，如 '<work>/q_*.png'")
    ap.add_argument("--out", help="items.json 路径，默认 <dir>/items.json")
    ap.add_argument("--force", action="store_true", help="允许覆盖已有 items.json")
    a = ap.parse_args(argv)

    out = Path(a.dir)
    if not out.is_dir():
        print("目录不存在：%s" % out)
        return 2
    items_file = Path(a.out) if a.out else out / "items.json"
    if items_file.exists() and not a.force:
        print("items.json 已存在，不覆盖（转写成果可能已经在里面）：%s" % items_file)
        print("真要重建加 --force")
        return 0

    rows, source = collect(out, a.images)
    if not rows:
        print("没找到题目：%s 里既没有 questions.json / crops.json，也没有匹配的裁片" % out)
        return 2
    data = {"title": _title(out), "subtitle": "", "items": rows}
    items_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    pdf = str(out / "做题本_矢量.pdf")
    task = out / "转写任务.md"
    task.write_text(TASK.format(n=len(rows), items=str(items_file), pdf=pdf), encoding="utf-8")

    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print("数据源：%s" % source)
    print("模板   -> %s（%d 题：%s）" % (
        items_file, len(rows), " / ".join("%s %d" % kv for kv in sorted(kinds.items()))))
    print("说明   -> %s" % task)
    print("下一步：agent 按说明逐题填写 items.json，再跑 reflow.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
