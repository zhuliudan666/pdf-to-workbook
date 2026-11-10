"""一键入口：probe -> 切题（按文本层自动选路线）-> crop -> qa -> structural -> review -> layout -> 验收。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import accept
import common
import crop
import expect
import layout
import probe
import qa
import report as report_mod   # 别叫 report：下面那个变量也叫 report（体检结果）
import review
import segment
import segment_ocr
import structural

HERE = Path(__file__).resolve().parent.parent


def main(pdf: str, cfg_path: str | None = None, out_dir: str | None = None, out_pdf: str | None = None) -> str:
    cfg_path = cfg_path or str(HERE / "config.yaml")
    out = Path(out_dir or (Path(pdf).parent / "workbook_out"))
    out.mkdir(parents=True, exist_ok=True)

    print("== 1/8 体检 ==")
    report = probe.main(pdf, cfg_path, str(out))
    v = report.get("verdict") or {}
    if v.get("level") == "no-go":
        print()
        for r in v.get("reasons", []):
            print("  - " + r)
        raise SystemExit(
            "\n体检结论是 NO-GO，已停止。这些问题绕不过去——"
            "在错的版面上切，切点算法再准也只是切得整齐而已。"
        )

    # 走哪条切题路线由体检结论决定，不让调用者自己记：扫描件拿 segment.py 跑，
    # 结果是一题都切不出来（它读的是文字层）。这一条踩过——手工按顺序敲命令，
    # 漏了 crop 那一步就 KeyError 'height'，敲错 segment 就是「切出 0 题」而不知道为什么。
    # 「ocr」也必须走 OCR：那层文字是扫描件后面加的，坐标和字都不可信。
    layer = report.get("text_layer")
    scan = layer in ("none", "ocr")
    print("\n== 2/8 切题（%s）==" % ("扫描件 · 条带 OCR" if scan else "文本层 · 正则"))
    questions = (segment_ocr.main if scan else segment.main)(pdf, cfg_path, str(out))
    if not questions:
        raise SystemExit(
            "一题都没切出来，先看 probe.md 里哪条正则命中最多，改 config.yaml 的 question_patterns"
            if not scan else
            "一题都没切出来：核对 ocr.strip_x0/strip_x1 是不是压在题号那一栏上，"
            "以及 question_patterns 是否认得出「1.[2008 数学一]」这种写法"
        )

    print("\n== 3/8 裁图 ==")
    crop.main(pdf, cfg_path, str(out))

    # 切点质检放在最前面：它只用数字就能判出「切在窄缝里 / 切穿 / 贴边」，
    # 一次把 22 道题的边界问题列成表。改切点逻辑的人看这一张表就够了，
    # 不用像以前那样把每道题都渲染出来看一遍。
    print("\n== 4/8 切点质检 ==")
    q = qa.main(pdf, cfg_path, str(out))

    # 结构校验放在校对单前面：题号断了比切点切歪严重得多，而缩略图看不出来
    print("\n== 5/8 结构校验 ==")
    struct = structural.check(questions, report.get("pages"))
    # 「题号连不连得上」之外，还要问「这本书应该有几道题」。
    # 少切一整节的书，每张图都完整，只有这个数能当场把它拦下来。
    spec = expect.scan(report.get("pdf"), out, common.load_config(cfg_path),
                       report.get("text_layer", "auto"))
    struct["expected"] = spec
    qprob, qwarn, qinfo = expect.check(len(questions), spec)
    if qprob:
        struct["problems"].append(qprob)
    (out / "structural.json").write_text(
        json.dumps(struct, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if qinfo:
        print("  " + qinfo)
    if qwarn:
        print("  ⚠ " + qwarn)
    highs = [p for p in struct["problems"] if p["severity"] == "high"]
    if highs:
        print(f"!! 发现 {len(highs)} 处高危：")
        for p in highs[:6]:
            print(f"   {p['id']} p{p['source_page']} {p['kind']} —— {p['detail']}")
    else:
        print(f"题号连续、无重号（{len(questions)} 题）")

    print("\n== 6/8 校对单 ==")
    review.main(pdf, cfg_path, str(out), struct=struct)

    print("\n== 7/8 拼版面 ==")
    target = layout.main(pdf, cfg_path, str(out), out_pdf)

    # 排版代码只知道自己画了什么。页眉写错书名、目录页码偏移、章节页没画，
    # 在它眼里都是「正常执行完毕」——必须把成品读回来再看一遍。
    print("\n== 8/8 验收 ==")
    acc = accept.main(target, cfg_path, str(out))

    # 判断、默认值、没验到的地方随成品落盘；否则这些只活在聊天里，换台机器就没了。
    report_mod.main(target, cfg_path, str(out))
    print(f"\n完成：{target}")
    if not acc["ok"]:
        print("（验收有硬伤，先修再交付：%d 项）" % len(acc["problems"]))
    # 中途产物默认不删（删一次要人点一次确认框，见 SKILL.md）。那就把账报清楚：
    # 这本占了多少、其中多少是可清的，用户自己决定要不要跑 clean。
    _report_footprint(out, target)
    return target


def _dir_mb(p: Path) -> float:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total / 1048576


def _report_footprint(out: Path, target) -> None:
    if not out.is_dir():
        return
    target = Path(target)
    crops = out / "crops"
    ocr = out / "ocr"
    tbl = out / "联系表.png"
    junk = sum(_dir_mb(p) for p in (crops, ocr) if p.is_dir())
    junk += tbl.stat().st_size / 1048576 if tbl.is_file() else 0
    try:
        deliverable = target.stat().st_size / 1048576
    except OSError:
        deliverable = 0.0
    total = _dir_mb(out)
    print(f"\n目录占用：{total:.1f} MB（成品 {deliverable:.1f} MB，可清 {junk:.1f} MB）")
    if junk >= 5:
        print(f"  可清的这些留着不影响下次重跑（同名覆盖）。要收干净："
              f"python tools/clean.py \"{out}\"")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python build.py <习题册.pdf> [config.yaml] [输出目录] [输出pdf]")
    a = sys.argv
    # 位置参数，没有 --config。写成 --config xxx 时它会被当成配置路径，
    # 后面的路径整体前移一位，报出来的却是「目录已存在」或者配置读不到——
    # 跟真实原因完全不搭边。宁可直接拦。
    for i, v in enumerate(a[2:5], start=2):
        if v.startswith("-"):
            raise SystemExit(
                f"build.py 只收位置参数，没有 {v}。\n"
                "用法: python build.py <习题册.pdf> [config.yaml] [输出目录] [输出pdf]"
            )
    main(a[1], a[2] if len(a) > 2 else None, a[3] if len(a) > 3 else None, a[4] if len(a) > 4 else None)
