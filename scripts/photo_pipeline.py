"""照片 → 做题本（单页版）

输入一张手机拍的习题册照片，输出 A4 打印用做题本 PDF。
与扫描件链路（segment_ocr.py）的区别：这里多了光照归一化、去斜、白场校正，
因为手机照片有阴影、透视和水印，行墨量判据必须先把亮度拉平才可靠。

用法：
  python photo_pipeline.py --src page.jpg --prefix 18 --fill 18.3,18.4,18.5 --out out.pdf

中间产物（flat/rot/ocr/crops）留在 --work 目录，便于核对切点。
"""
import argparse, json, os, re
from itertools import combinations

import cv2
import numpy as np
import fitz
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

MM = 72 / 25.4


def imread_u(path, flag=cv2.IMREAD_COLOR):
    return cv2.imdecode(np.fromfile(path, np.uint8), flag)


def imwrite_u(path, img):
    cv2.imencode('.png', img)[1].tofile(path)


def flatten(gray):
    """大核闭运算估计低频亮度场，相除去掉阴影与光照不均。"""
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (61, 61))
    bg = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, k)
    bg = cv2.GaussianBlur(bg, (0, 0), 25)
    out = gray.astype(np.float32) / np.maximum(bg.astype(np.float32), 1) * 235.0
    return np.clip(out, 0, 255).astype(np.uint8)


def deskew(img, lo=-7, hi=7, step=0.1):
    """投影方差最大化找倾角。用二值反相图的按行求和离散度衡量。"""
    H, W = img.shape[:2]
    _, bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    body = bw.copy()
    body[:int(H * 0.03), :] = 0
    body[int(H * 0.74):, :] = 0
    body[:, int(W * 0.9):] = 0
    best, bang = -1, 0.0
    for ang in np.arange(lo, hi + 1e-9, step):
        M = cv2.getRotationMatrix2D((W / 2, H / 2), ang, 1.0)
        r = cv2.warpAffine(body, M, (W, H), flags=cv2.INTER_NEAREST, borderValue=0)
        v = r.sum(axis=1).astype(np.float64).var()
        if v > best:
            best, bang = v, ang
    M = cv2.getRotationMatrix2D((W / 2, H / 2), bang, 1.0)
    return cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE), bang


def ocr_rows(img, scale=1.5):
    eng = RapidOCR()
    big = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    res, _ = eng(big, box_thresh=0.3, unclip_ratio=1.8, text_score=0.3)
    rows = []
    for box, text, score in (res or []):
        xs = [p[0] / scale for p in box]
        ys = [p[1] / scale for p in box]
        rows.append({'text': text, 'score': float(score),
                     'x0': float(min(xs)), 'y0': float(min(ys)),
                     'x1': float(max(xs)), 'y1': float(max(ys))})
    rows.sort(key=lambda r: (r['y0'], r['x0']))
    return rows


def find_questions(rows, prefix, y_max):
    pat = re.compile(r'^\D{0,3}%s\s*[.．]\s*(\d{1,2})(?!\d)' % re.escape(prefix))
    out = []
    for r in rows:
        m = pat.search(r['text'][:14].strip())
        if m and r['y0'] < y_max:
            out.append((int(m.group(1)), r['y0']))
    out.sort(key=lambda t: t[1])
    return out


def make_cuts(gray, rows, qs, x_body, thr=140, blank_th=4.0, min_len=3, win=30.0):
    """切点吸附：在题号上方 win 像素窗口内取最靠下的整行空白带；
    且不得越过上一题最后一个 OCR 框的底边（否则大分式会被劈开）。"""
    H, W = gray.shape
    x0, x1 = x_body
    ink = (gray[:, x0:x1] < thr).sum(axis=1).astype(np.float32)
    prof = np.convolve(ink, np.ones(3) / 3, mode='same')
    blank = prof < blank_th
    gaps, s = [], None
    for y in range(H):
        if blank[y] and s is None:
            s = y
        elif not blank[y] and s is not None:
            if y - s >= min_len:
                gaps.append((s, y))
            s = None
    if s is not None and H - s >= min_len:
        gaps.append((s, H))

    boxes = [(r['y0'], r['y1'], r['x1']) for r in rows]

    def prev_bottom(yq):
        ys = [b for (a, b, xx) in boxes if a < yq - 5 and xx > 120 and b < yq + 5]
        return max(ys) if ys else 0.0

    cuts = []
    for _n, yq in qs:
        lo = max(yq - win, prev_bottom(yq) + 2.0)
        cand = [gp for gp in gaps if gp[1] >= lo and gp[0] <= yq + 2]
        if not cand:
            cuts.append(max(lo, yq - 6.0))
            continue
        gp = max(cand, key=lambda g: g[0])
        cuts.append((max(gp[0], lo) + gp[1]) / 2.0)
    return cuts, gaps


def vtrim(img, thr=150, pad=4):
    nz = np.where((img < thr).sum(axis=1) > 2)[0]
    if not len(nz):
        return img
    return img[max(0, nz[0] - pad):min(img.shape[0], nz[-1] + pad + 1)]


def whiten(img, hi=200, lo=25):
    return np.clip((img.astype(np.float32) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True)
    ap.add_argument('--prefix', default='18')
    ap.add_argument('--fill', default='', help='逗号分隔的填空题号，如 18.3,18.4')
    ap.add_argument('--gap-fill', type=float, default=35)
    ap.add_argument('--gap-solve', type=float, default=65)
    ap.add_argument('--title', default='')
    ap.add_argument('--xbody', default='', help='正文列区间 x0,x1；留空自动取 4%..94%')
    ap.add_argument('--yend', type=float, default=0, help='题目区下边界，0=自动')
    ap.add_argument('--out', required=True)
    ap.add_argument('--work', default='')
    a = ap.parse_args()

    work = a.work or (os.path.splitext(a.out)[0] + '_work')
    os.makedirs(work, exist_ok=True)

    img = imread_u(a.src)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    flat = flatten(gray)
    rot, ang = deskew(flat)
    rot = cv2.medianBlur(rot, 3)
    imwrite_u(os.path.join(work, 'rot.png'), rot)
    H, W = rot.shape
    print('deskew=%.1f deg  size=%dx%d' % (ang, W, H))

    rows = ocr_rows(rot)
    json.dump(rows, open(os.path.join(work, 'ocr.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    y_max = a.yend if a.yend > 0 else H
    qs = find_questions(rows, a.prefix, y_max)
    if not qs:
        raise SystemExit('未识别到题号，检查 --prefix / 图片质量')
    print('题号: %s' % ', '.join('%s.%d' % (a.prefix, n) for n, _ in qs))

    if a.xbody:
        xb = tuple(int(v) for v in a.xbody.split(','))
    else:
        xb = (int(W * 0.04), int(W * 0.94))
    cuts, _gaps = make_cuts(rot, rows, qs, xb)

    # 末题下界：最后一条空白带起点，或题号区末尾
    tail = [g for g in _gaps if g[0] > qs[-1][1]]
    end = (tail[0][0] + 3) if tail else (y_max if a.yend > 0 else H * 0.72)
    cuts.append(float(end))

    crops, heights = [], []
    XL, XR = xb[0] - 20, xb[1] + 30
    for i, (n, _yq) in enumerate(qs):
        top, bot = int(cuts[i]) + 2, int(cuts[i + 1]) - 2
        sub = whiten(vtrim(rot[max(0, top):min(H, bot), XL:min(W, XR)]))
        p = os.path.join(work, 'q_%s_%d.png' % (a.prefix, n))
        imwrite_u(p, sub)
        crops.append((''.join([a.prefix, '.', str(n)]), p, sub.shape[1], sub.shape[0]))

    fill_set = set(x.strip() for x in a.fill.split(',') if x.strip())
    mm = 72 / 25.4
    PW, PH = 210 * mm, 297 * mm
    ML = MR = 20 * mm
    MT, MB = 18 * mm, 16 * mm
    CW, AVAIL = PW - ML - MR, PH - MT - MB

    def gap_of(no):
        return (a.gap_fill if no in fill_set else a.gap_solve) * mm

    hs = [CW * h / w + gap_of(no) for no, _p, w, h in crops]
    k, cur = 1, 0.0
    for h in hs:
        if cur + h > AVAIL:
            k += 1
            cur = 0.0
        cur += h
    best = None
    for cs in combinations(range(1, len(hs)), k - 1):
        segs, prev = [], 0
        for c in tuple(cs) + (len(hs),):
            segs.append(sum(hs[prev:c]))
            prev = c
        m = max(segs)
        if m <= AVAIL and (best is None or m < best[0]):
            best = (m, cs)
    if best is None:
        raise SystemExit('排版失败：题图过高，检查 --gap-* 或裁切范围')
    _m, cs = best
    plans, prev = [], 0
    for c in tuple(cs) + (len(hs),):
        plans.append((list(range(prev, c)), sum(hs[prev:c])))
        prev = c

    font = r'C:\Windows\Fonts\msyh.ttc'
    if not os.path.exists(font):
        font = r'C:\Windows\Fonts\simsun.ttc'
    doc = fitz.open()
    for idx, (idxs, tot) in enumerate(plans):
        page = doc.new_page(width=PW, height=PH)
        if a.title:
            page.insert_text((ML, MT - 7 * mm), a.title, fontsize=7.5, fontfile=font,
                             fontname='cn', color=(0.42, 0.42, 0.42))
            page.draw_line(fitz.Point(ML, MT - 4 * mm), fitz.Point(PW - MR, MT - 4 * mm),
                           color=(0.82, 0.82, 0.82), width=0.4)
        extra = min(20 * mm, max(0.0, (AVAIL - tot) / len(idxs)))
        y = MT
        for i in idxs:
            no, p, w, h = crops[i]
            real = CW * h / w
            page.insert_image(fitz.Rect(ML, y, ML + CW, y + real), stream=open(p, 'rb').read())
            y += real + gap_of(no) + extra
        page.insert_text((PW - MR - 42, PH - MB + 9 * mm),
                         '第 %d 页 / 共 %d 页' % (idx + 1, len(plans)),
                         fontsize=7.5, fontfile=font, fontname='cn', color=(0.42, 0.42, 0.42))
    doc.subset_fonts()
    doc.save(a.out, garbage=4, deflate=True, clean=True)
    print('输出 %s  页数=%d  体积=%.2fMB' % (a.out, doc.page_count, os.path.getsize(a.out) / 1e6))
    for i, (idxs, tot) in enumerate(plans):
        print('  第%d页 %s' % (i + 1, ', '.join(crops[j][0] for j in idxs)))


if __name__ == '__main__':
    main()
