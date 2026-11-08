"""把 PDF 每页渲染成 PNG，用于肉眼检查版面（也方便贴给用户看）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf


def main(pdf: str, out_dir: str | None = None, dpi: int = 110, pages: str | None = None) -> list[str]:
    src = Path(pdf)
    out = Path(out_dir or src.parent / (src.stem + "_preview"))
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf)
    wanted = None
    if pages:
        wanted = {int(x) - 1 for x in pages.split(",")}
    files = []
    for i, page in enumerate(doc):
        if wanted is not None and i not in wanted:
            continue
        pix = page.get_pixmap(dpi=dpi)
        f = out / f"page_{i + 1:02d}.png"
        pix.save(f)
        files.append(str(f))
    doc.close()
    print("\n".join(files))
    return files


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python render_preview.py <pdf> [输出目录] [dpi] [页码,逗号分隔]")
    main(
        sys.argv[1],
        sys.argv[2] if len(sys.argv) > 2 else None,
        int(sys.argv[3]) if len(sys.argv) > 3 else 110,
        sys.argv[4] if len(sys.argv) > 4 else None,
    )
