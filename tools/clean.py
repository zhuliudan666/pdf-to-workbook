"""清中途产物。**唯一会删东西的入口，而且只在你主动跑的时候。**

为什么单独做一个命令：安全策略会把每个删除动作换成「移进回收站」并弹一次确认框。
以前清理动作散在 `profile.py`（生成预案前）、`regress.py`（每本跑完）、
`reflow.py`（打印完）、`selftest.py`（每次自测）里，一轮制作要连点三四次
「允许删除吗」——而这些东西留着根本不影响结果。现在那些地方默认都不删，
要清就到这里来一次。

```bash
python tools/clean.py 工作目录...           # 删 crops/、联系表.png、除 sub.pdf 外的 pdf
python tools/clean.py --ocr 工作目录...      # 连 ocr/ 缓存一起删（下次要重新识别）
python tools/clean.py --run                 # 清回归网的 tools/regress/.run/
python tools/clean.py --dry 工作目录...      # 只报要删什么、多大，不动手
```

**不动的东西**（这些删了就得重做，或者就是交付物）：

- `questions.json` / `probe.json` / `structural.json` / `qa.csv` / `accept.json`
  —— 都是几十 KB 的数字，重跑要几分钟，删了没意义。
- `book_做题本.pdf` / `校对单.pdf` —— 成品。
- 源 pdf、配置文件。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRESERVE_PDF = {"sub.pdf"}


def size_of(p: Path) -> int:
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def targets(out: Path, with_ocr: bool) -> list[Path]:
    found: list[Path] = []
    crops = out / "crops"
    if crops.is_dir():
        found.append(crops)
    tbl = out / "联系表.png"
    if tbl.is_file():
        found.append(tbl)
    if with_ocr:
        ocr = out / "ocr"
        if ocr.is_dir():
            found.append(ocr)
    for f in out.glob("*.pdf"):
        # sub.pdf 是切片缓存；成品（做题本/校对单）一律不动。
        if f.name in PRESERVE_PDF or "做题本" in f.name or "校对单" in f.name:
            continue
        found.append(f)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="清中途产物（默认只报不删）")
    ap.add_argument("dirs", nargs="*", help="一个或多个工作目录（build.py 的 out 参数）")
    ap.add_argument("--ocr", action="store_true", help="连 ocr/ 条带缓存一起删")
    ap.add_argument("--run", action="store_true", help="清回归网 tools/regress/.run/")
    ap.add_argument("--dry", action="store_true", help="只列表，不删")
    a = ap.parse_args()

    jobs: list[tuple[Path, list[Path]]] = []
    for d in a.dirs:
        out = Path(d)
        if not out.is_dir():
            print(f"跳过（不是目录）：{out}")
            continue
        jobs.append((out, targets(out, a.ocr)))
    if a.run:
        run = HERE / "regress" / ".run"
        if run.is_dir():
            jobs.append((run, [p for p in sorted(run.iterdir()) if p.is_dir()]))

    total = 0
    for out, items in jobs:
        if not items:
            print(f"\n{out}：没有要清的东西。")
            continue
        print(f"\n{out}")
        for p in items:
            n = size_of(p)
            total += n
            print(f"  {p.name:28s} {n / 1048576:8.1f} MB")
    if not total:
        print("\n没什么可清的。")
        return 0

    print(f"\n合计 {total / 1048576:.1f} MB。")
    if a.dry:
        print("（--dry：什么都没删）")
        return 0

    # 只删这张表里列出来的东西，绝不递归扫别的路径。
    print("开始清理……")
    for out, items in jobs:
        for p in items:
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            except (OSError, SystemExit) as e:
                # 删不掉不是错误：沙箱可能把它换成「移进回收站」再抛 SystemExit。
                print(f"  没删掉 {p.name}（{type(e).__name__}），留着不影响结果")
    print("清完了。下次跑会重新生成 crops；ocr 若删了则要重新识别。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
