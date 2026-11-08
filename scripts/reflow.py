# -*- coding: utf-8 -*-
"""把「题目 LaTeX 数据」排成 A4 做题本 PDF —— 矢量路线。

和 crop.py 的分工：
  crop.py    位图路线：保留原书版式与字体，缺点是受输入分辨率限制（照片约 130-150dpi）
  reflow.py  矢量路线：文字与公式全部重新排版，打印任意清晰，缺点是内容需先转成 LaTeX

矢量路线的第一步「题目转成 LaTeX」不在本脚本里做。扫描件/照片的公式混排，
通用 OCR 无法处理（会把公式整段丢掉），必须由多模态模型逐题转写。见 SKILL.md。

用法：
  python reflow.py items.json -o 做题本.pdf
  python reflow.py items.json -o 做题本.pdf --fill-gap 33 --solve-gap 62

items.json 结构：
{
  "title":    "2026 考研数学基础 30 讲 · 高等数学分册",
  "subtitle": "第 18 讲 曲线积分与曲面积分",
  "items": [
    {"no": "18.3", "kind": "fill",
     "stem": "设 $\\Gamma: x=\\cos t$，则",
     "disp": "$\\displaystyle \\int_\\Gamma (x^2+y^2+z^2)\\,\\mathrm{d}s=\\underline{\\qquad}$",
     "subs": [],
     "src_image": "crops/18_3.png",
     "uncertain": ["分子 $e^{x^2}$ 原图模糊，可能是 $e^{x}$"]}
  ]
}
  kind: fill（填空）| solve（解答）| choice（选择）
  stem: 中文与行内公式混排，$...$ 包公式
  disp: 可选，独立成行居中的公式
  subs: 可选，如 ["(1) 证明 …", "(2) 求 …"]
  src_image: 可选，原图裁片路径（相对 items.json 或绝对）。有它，校对单才能左右对照
  uncertain: 可选，转写时拿不准的地方，一句一条。写进去才会出现在校对单上——
             只在聊天里说「这题我不确定」等于没说，交付物里必须有痕迹。

依赖：Chrome 或 Edge（headless 打印）、联网（KaTeX CDN）。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

HTML_TPL = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>__TITLE__</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
<style>
  @page { size: A4; margin: 0; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: #fff; }
  body { font-family: "SimSun","Songti SC","Noto Serif CJK SC",serif;
         font-size: 10.5pt; line-height: 1.9; color: #000; }
  /* 不能用 display:none —— 那样子元素量不到高度，分页会全部挤进一页 */
  #stage { position: absolute; top: 0; left: 0; width: 170mm; visibility: hidden; }
  #probe { height: 1mm; width: 1mm; position: absolute; visibility: hidden; }
  .page { position: relative; width: 210mm; height: 297mm;
          padding: 20mm 20mm 16mm 20mm; page-break-after: always; overflow: hidden; }
  .page:last-child { page-break-after: auto; }
  .hdr { position: absolute; top: 10mm; left: 20mm; right: 20mm;
         display: flex; justify-content: space-between; font-size: 8.5pt; color: #444;
         padding-bottom: 1.2mm; border-bottom: 0.4pt solid #bbb; }
  .ftr { position: absolute; bottom: 8mm; left: 0; right: 0; text-align: center;
         font-size: 8.5pt; color: #444; font-family: "Times New Roman", serif; }
  .q { break-inside: avoid; }
  .q-line { display: flex; align-items: flex-start; }
  .no { flex: 0 0 14mm; width: 14mm; font-family: "Times New Roman", serif;
        font-weight: bold; font-size: 11pt; line-height: 1.9; }
  .stem { flex: 1; }
  .disp { margin: 1.5mm 0 0 0; text-align: center; font-size: 11pt; }
  .disp .katex-display { margin: 0; }
  .sub { margin-top: 1mm; }
  .gap { width: 100%; }
  .katex { font-size: 1.06em; }
  .en { font-family: "Times New Roman", serif; }
</style></head>
<body>
<div id="probe"></div>
<div id="stage">
__ITEMS__
</div>
<script>
(function () {
  var TITLE = __TITLE_JSON__, SUB = __SUB_JSON__;
  var GAP = { fill: __GAP_FILL__, solve: __GAP_SOLVE__, choice: __GAP_CHOICE__ };
  var QGAP = __QGAP__;   // 题与题之间
  renderMathInElement(document.getElementById('stage'), {
    delimiters: [{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}],
    throwOnError: false
  });
  var mm = document.getElementById('probe').getBoundingClientRect().height;
  var H = 297 * mm, PAD_T = 20 * mm, PAD_B = 16 * mm, HDR = 9 * mm;
  var avail = H - PAD_T - PAD_B - HDR;
  var stage = document.getElementById('stage');
  var blocks = Array.prototype.slice.call(stage.children);
  var pages = [], cur = [], curH = 0;
  blocks.forEach(function (b) {
    var h = b.getBoundingClientRect().height + QGAP * mm;
    if (cur.length && curH + h > avail) { pages.push(cur); cur = []; curH = 0; }
    cur.push(b); curH += h;
  });
  if (cur.length) pages.push(cur);
  var body = document.body;
  pages.forEach(function (items, i) {
    var p = document.createElement('div');
    p.className = 'page';
    var hdr = document.createElement('div');
    hdr.className = 'hdr';
    hdr.innerHTML = '<span></span><span></span>';
    hdr.children[0].textContent = TITLE;
    hdr.children[1].textContent = SUB;
    p.appendChild(hdr);
    items.forEach(function (el, j) {
      if (j > 0) el.style.marginTop = QGAP + 'mm';
      p.appendChild(el);
    });
    var ftr = document.createElement('div');
    ftr.className = 'ftr';
    ftr.textContent = '— ' + (i + 1) + ' —';
    p.appendChild(ftr);
    body.appendChild(p);
  });
  stage.remove();
  document.getElementById('probe').remove();
  document.title = '__DONE__';
})();
</script>
</body></html>
"""

ITEM_TPL = """<div class="q">
  <div class="q-line"><div class="no">{no}</div><div class="stem">{stem}{disp}{subs}</div></div>
  <div class="gap" style="height:{gap}mm"></div>
</div>"""


def find_browser():
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    raise SystemExit("找不到 Chrome / Edge，无法打印 PDF")


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_html(data, gaps, qgap):
    items = []
    for it in data["items"]:
        kind = it.get("kind", "fill")
        gap = gaps.get(kind, gaps["fill"])
        stem = esc(it.get("stem", ""))
        stem = stem.replace("$", "$")  # KaTeX 需要原始 $
        # src 里公式是 $...$，转义后 $ 不变，& 已转义
        disp = ""
        if it.get("disp"):
            disp = '<div class="disp">%s</div>' % it["disp"]
        subs = ""
        if it.get("subs"):
            subs = "".join('<div class="sub">%s</div>' % s for s in it["subs"])
        items.append(ITEM_TPL.format(no=esc(it["no"]), stem=stem, disp=disp, subs=subs, gap=gap))
    html = (HTML_TPL
            .replace("__ITEMS__", "\n".join(items))
            .replace("__TITLE__", esc(data.get("title", "")))
            .replace("__TITLE_JSON__", json.dumps(data.get("title", ""), ensure_ascii=False))
            .replace("__SUB_JSON__", json.dumps(data.get("subtitle", ""), ensure_ascii=False))
            .replace("__GAP_FILL__", str(gaps["fill"]))
            .replace("__GAP_SOLVE__", str(gaps["solve"]))
            .replace("__GAP_CHOICE__", str(gaps["choice"]))
            .replace("__QGAP__", str(qgap))
            .replace("__DONE__", "READY"))
    return html


REVIEW_TPL = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>__TITLE__</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
<style>
  @page { size: A4; margin: 13mm 13mm 11mm; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: "SimSun","Songti SC",serif; font-size: 10pt; line-height: 1.75; color: #111; }
  h1 { font-size: 14pt; margin: 0 0 1.5mm; font-weight: 600; }
  .meta { font-size: 8.5pt; color: #444; line-height: 1.6; margin-bottom: 4mm; padding-bottom: 2mm;
          border-bottom: 0.5pt solid #ccc; }
  .warn { color: #b71c1c; }
  .ok { color: #1b5e20; }
  .card { border: 0.6pt solid #ccc; border-radius: 1.5mm; padding: 2.6mm 3mm; margin-bottom: 3.4mm;
          break-inside: avoid; }
  .card.bad { border: 1pt solid #c62828; background: #fffbfb; }
  .head { display: flex; align-items: baseline; gap: 2.5mm; margin-bottom: 1.6mm; }
  .no { font-family: "Times New Roman", serif; font-weight: bold; font-size: 11.5pt; }
  .badge { font-size: 8pt; color: #b71c1c; border: 0.5pt solid #d99; border-radius: 1mm;
           padding: 0 1.4mm; background: #fdecea; }
  .tag { font-size: 8pt; color: #666; }
  .body { margin-top: 1.2mm; }
  .lbl { font-size: 7.5pt; color: #888; letter-spacing: 0.5pt; margin: 1.6mm 0 0.8mm; }
  .shot { display: block; width: 100%; max-height: 78mm; object-fit: contain;
          object-position: left top; border: 0.4pt solid #ddd; background: #fff; }
  .miss { color: #aaa; font-size: 8pt; }
  .txt { }
  .disp { text-align: center; margin: 1mm 0; }
  .disp .katex-display { margin: 0; }
  .sub { margin-top: 0.8mm; }
  .flag { margin-top: 2mm; font-size: 8.5pt; color: #8e1b1b; background: #fff4f4;
          border-left: 2pt solid #c62828; padding: 1.4mm 2mm; line-height: 1.6; }
  .note { margin-top: 1.6mm; font-size: 8.5pt; color: #444; }
</style></head>
<body>
__BODY__
<script>
renderMathInElement(document.body, {
  delimiters: [{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}],
  throwOnError: false
});
document.title = 'READY';
</script>
</body></html>
"""


def _uncertain_list(it):
    out = []
    for u in it.get("uncertain") or []:
        if isinstance(u, dict):
            out.append((str(u.get("expr") or u.get("text") or ""), str(u.get("why") or "")))
        else:
            out.append((str(u), ""))
    return out


def _img_url(path, base):
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = Path(base) / p
    if not p.exists():
        return None
    return "file:///" + quote(str(p.resolve()).replace("\\", "/"), safe="/:")


def build_review_html(data, struct=None):
    """转写校对单：左原图、右转写，存疑的排最前面。

    这条路线没有位图可切，所以校对单的作用不是「看切点」，而是
    「逐题对照原图核公式」。这些题本来就只有转写版本，一旦认错符号，
    PDF 看起来完全自洽，做题做到一半才会发现条件不对。
    """
    items = list(data["items"])
    base = data.get("__base__", ".")
    bad = [it for it in items if _uncertain_list(it)]
    rest = [it for it in items if not _uncertain_list(it)]
    ordered = bad + rest

    head = [f"<h1>转写校对单 · {esc(data.get('subtitle') or data.get('title') or '')}</h1>"]
    meta = [f"共 {len(items)} 题　　存疑 {len(bad)} 题"]
    if struct:
        st = struct
        highs = [p for p in st["problems"] if p["severity"] == "high"]
        secs = list(st["sections"].values())
        if secs:
            s = secs[0]
            meta.append(f"题号 {s['first']} → {s['last']}")
        if highs:
            meta.append(f"<span class='warn'>题号有 {len(highs)} 处高危，先处理</span>")
        elif st["problems"]:
            meta.append("<span class='ok'>题号连续</span>")
        else:
            meta.append("<span class='ok'>题号连续，无重号</span>")
    head.append("<div class='meta'>" + "　·　".join(meta) +
                "<br>读法：上为原图，下为转写。红框 = 有存疑点，必须对照书本核；"
                "逐题比对公式的符号、上下标、系数，不必看排版。</div>")

    if not bad:
        head.append("<div class='meta ok'>没有登记存疑点。但「没登记」不等于「全对」——"
                    "公式密集的题仍建议抽看两三道。</div>")

    cards = []
    for it in ordered:
        flags = _uncertain_list(it)
        cls = "card bad" if flags else "card"
        kind = {"fill": "填空", "solve": "解答", "choice": "选择"}.get(it.get("kind", "fill"), "")
        badge = f"<span class='badge'>存疑 {len(flags)}</span>" if flags else ""
        url = _img_url(it.get("src_image"), base)
        shot = (f"<div class='lbl'>原图</div><img class='shot' src='{url}'>" if url
                else "")
        stem = esc(it.get("stem", ""))
        disp = f"<div class='disp'>{it['disp']}</div>" if it.get("disp") else ""
        subs = "".join(f"<div class='sub'>{s}</div>" for s in (it.get("subs") or []))
        flag_html = ""
        if flags:
            rows = "".join(
                f"<div>· {esc(e)}" + (f" —— {esc(w)}" if w else "") + "</div>" for e, w in flags
            )
            flag_html = f"<div class='flag'>{rows}</div>"
        note = f"<div class='note'>{esc(it['note'])}</div>" if it.get("note") else ""
        cards.append(
            f"<div class='{cls}'><div class='head'><span class='no'>{esc(it['no'])}</span>"
            f"{badge}<span class='tag'>{kind}</span></div>"
            f"{shot}<div class='lbl'>转写</div>"
            f"<div class='body'><div class='txt'>{stem}{disp}{subs}</div></div>"
            f"{flag_html}{note}</div>"
        )

    body = "\n".join(head + cards)
    return (REVIEW_TPL
            .replace("__BODY__", body)
            .replace("__TITLE__", esc(f"校对单 · {data.get('subtitle', '')}")))


def print_pdf(html: str, out: str) -> None:
    tmp = tempfile.mkdtemp(prefix="reflow_")
    hp = os.path.join(tmp, "page.html")
    with open(hp, "w", encoding="utf-8") as f:
        f.write(html)
    browser = find_browser()
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--allow-file-access-from-files", "--no-pdf-header-footer",
           "--virtual-time-budget=30000",
           "--print-to-pdf=%s" % os.path.abspath(out), "file:///" + hp.replace("\\", "/")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not os.path.isfile(out) or os.path.getsize(out) < 2000:
        sys.stderr.write(r.stdout + "\n" + r.stderr + "\n")
        raise SystemExit("打印失败：%s" % out)
    # 临时目录默认不删：安全策略会把删除换成「移进回收站」并弹确认框，
    # 用户得为一次打印点一次。系统临时目录本来就会自己清。
    if os.environ.get("PDF2WB_SWEEP") == "1":
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("items", help="items.json")
    ap.add_argument("-o", "--out", required=True, help="输出 PDF")
    ap.add_argument("--fill-gap", type=float, default=33, help="填空题留白 mm")
    ap.add_argument("--solve-gap", type=float, default=62, help="解答题留白 mm")
    ap.add_argument("--choice-gap", type=float, default=18, help="选择题留白 mm")
    ap.add_argument("--qgap", type=float, default=4, help="题间距 mm")
    ap.add_argument("--keep-html", action="store_true")
    ap.add_argument("--review-out", default=None, help="校对单路径，默认 <输出>_校对单.pdf")
    ap.add_argument("--no-review", action="store_true", help="不生成校对单")
    args = ap.parse_args()

    items_path = os.path.abspath(args.items)
    with open(items_path, encoding="utf-8") as f:
        data = json.load(f)
    data["__base__"] = os.path.dirname(items_path)
    gaps = {"fill": args.fill_gap, "solve": args.solve_gap, "choice": args.choice_gap}

    out = os.path.abspath(args.out)
    print_pdf(build_html(data, gaps, args.qgap), out)
    if args.keep_html:
        with open(os.path.splitext(out)[0] + ".html", "w", encoding="utf-8") as f:
            f.write(build_html(data, gaps, args.qgap))
    print("OK %s  %.0f KB  %d 题" % (out, os.path.getsize(out) / 1024, len(data["items"])))

    # 有存疑或原图就默认出校对单——这件事不能靠「记得加参数」
    wants = not args.no_review and (
        args.review_out or any(_uncertain_list(it) or it.get("src_image") for it in data["items"])
    )
    if wants:
        import structural

        st = structural.check([{"id": it["no"], "seq": i + 1} for i, it in enumerate(data["items"])],
                              geometry=False)
        rp = args.review_out or os.path.splitext(out)[0] + "_校对单.pdf"
        print_pdf(build_review_html(data, st), rp)
        n_bad = sum(1 for it in data["items"] if _uncertain_list(it))
        print("校对单 %s  %.0f KB  存疑 %d 题" % (rp, os.path.getsize(rp) / 1024, n_bad))


if __name__ == "__main__":
    main()
