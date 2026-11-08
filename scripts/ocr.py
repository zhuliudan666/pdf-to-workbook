"""对扫描版 PDF 做 OCR，结果按页缓存成 JSON（坐标已换算成 PDF 点）。

扫描件没有文本层，题号正则全都失效，必须先拿到 (文字, 坐标)。
缓存是关键：OCR 一页要 5-8 秒，整本上百页，重跑不能重识别。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

_ENGINE = None


def engine() -> RapidOCR:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RapidOCR()
    return _ENGINE


def ocr_region(page, clip, dpi: int = 300) -> list[dict]:
    """只识别页面上的一个矩形区域，坐标换算回 PDF 点。"""
    return _run(page.get_pixmap(dpi=dpi, clip=pymupdf.Rect(*clip)), dpi)


def ocr_page(page, dpi: int = 200) -> list[dict]:
    return _run(page.get_pixmap(dpi=dpi), dpi)


def _run(pix, dpi: int) -> list[dict]:
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    arr = np.array(img)[:, :, ::-1]
    result, _ = engine()(arr)
    scale = 72.0 / dpi
    lines = []
    for box, text, score in result or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(
            {
                "text": text,
                "box": [min(xs) * scale, min(ys) * scale, max(xs) * scale, max(ys) * scale],
                "score": float(score) if score is not None else 1.0,
            }
        )
    lines.sort(key=lambda r: (round(r["box"][1] / 6), r["box"][0]))
    return lines


def ensure(pdf: str, page_no: int, out_dir: Path, dpi: int = 200, force: bool = False) -> dict:
    cache = out_dir / "ocr" / f"page_{page_no:03d}.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text(encoding="utf-8"))
    doc = pymupdf.open(pdf)
    page = doc[page_no - 1]
    data = {"page": page_no, "dpi": dpi, "lines": ocr_page(page, dpi)}
    doc.close()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def main(pdf: str, out_dir: str, first: int, last: int, dpi: int = 200) -> None:
    out = Path(out_dir)
    doc = pymupdf.open(pdf)
    total = len(doc)
    doc.close()
    last = min(last, total)
    t0 = time.time()
    for pno in range(first, last + 1):
        data = ensure(pdf, pno, out, dpi)
        print(f"P{pno}  {len(data['lines'])} 行  {time.time() - t0:.0f}s")
    print(f"完成 {first}-{last}，共 {last - first + 1} 页")


if __name__ == "__main__":
    if len(sys.argv) < 5:
        raise SystemExit("用法: python ocr.py <pdf> <输出目录> <起始页> <结束页> [dpi]")
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]),
         int(sys.argv[5]) if len(sys.argv) > 5 else 200)
