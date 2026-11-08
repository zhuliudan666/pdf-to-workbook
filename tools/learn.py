"""把一次做完的书沉淀成预案：profiles/<id>.yaml

为什么要有这一步：判据、手册、验收都在积累，但「这本书的配置是怎么调出来的」没有积累——
下一本同类书还是从默认值起手，把同样的试错再走一遍。classify 认不出类型时只会提示
「跑一次 probe，把结论补成 profiles/ 里的新预案」，然后要人手工抄正则、抄阈值，
而手抄最容易错的正是 `^` 和转义。

这个命令做三件事，顺序不能换：

1. **读回结果，不重跑流水线**：probe.json 的指纹、配置文件的生效值（intake.yaml
   或这次的 cfg）、questions.json 的题数、accept.json 的验收结论、qa.csv 的标记。
2. **按指纹推 when 判据，松紧用回归网验**：先取最宽的一档，拿 7 本基线逐本试——
   谁被误认就把这一档收窄一格。宁可窄到只认「这一类的样子」，也不要宽到把别的书抢过来；
   分类抢错比认不出更糟，认不出只走默认值，抢错就直接把上本书的调参结果套上去。
3. **只带这一类书通用的配置**。页段、章名、页眉书名、条带几何一律不带：它们是
   **这本书的账**，抄到下一本上就是事故——页眉写死过别人的书名，content_box 抄错
   会把悬挂式题号切掉半截，而这两种失败都不会报任何错。

用法：

```bash
# 新增一类（没人认过这种版式）
python tools/learn.py 输出目录 --cfg 配置.yaml --id 新类型 --label "一句话描述"

# 已知类型：把这次的调参结果并进已有预案（同一个 id 不要另立门户）
python tools/learn.py 输出目录 --cfg 配置.yaml --merge bank-book

# 只看会写出什么，不落盘
python tools/learn.py 输出目录 --cfg 配置.yaml --id xxx --dry
```

**同类型的第 8 本书应该走 --merge，不要新建**：两个预案的 when 会互相抢，
而 classify 只取「条件最多的一条」，抢起来是隐性的，不会报错。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(HERE / "tools"))

import common  # noqa: E402
import classify  # noqa: E402

PROFILES = HERE / "profiles"
BASE = HERE / "tools" / "regress"

# 可以跟着「类型」走的配置。没有列在这里的一律不带——
# 少带一项只是让下本书多调一次，多带一项可能是直接切错。
CARRY = (
    "question_patterns",            # 题号长什么样由这类素材说了算（替换）
    "cut_stop_patterns",            # 切点停在哪（追加）
    "ignore_patterns",              # 页码/水印这类基础忽略项（追加）
    "answer_gap_floor_by_section",  # 物理下限，跟纸张和字体大小相关
    "answer_gap_cap_by_section",    # 封顶，同上
    "image",                        # 二值化模式与阈值
    "text_layer",                   # 要不要盖不可见文字层
    "cross_page",                   # 跨页续文要不要合并
)

# 不带的项要能说出「为什么不带」，否则下次还会有人想把它加回来
DROP_WHY = {
    "pages": "页段是这本书的账，下本书的习题在别的页上",
    "chapters": "章名 + 页段都是这本书的账，抄过去等于给下本书盖章",
    "catalog": "章名是这本书的账",
    "header": "页眉书名必须从原书读；写死过一次，高数分册印着「线代真题复盘」",
    "content_box": "版心是几何量，必须和 ocr.strip_x0 成套改，单抄一个会切掉题号半截",
    "ocr": "条带几何（strip_x0/x1/dpi），每本书的页面边距都不一样",
    "paper": "纸张与边距跟装订方式走（用户选 print 档位）",
    "page_quota": "由 room 档位覆盖，写进预案反而跟用户选的档位打架",
    "page_quota_mixed": "同上，由 room 推导",
    "gap_style": "用户在开场选的（空白/横线/格子）",
    "answer_gap_floor_mm": "已被 answer_gap_floor_by_section 覆盖",
    "answer_gap_cap_mm": "已被 answer_gap_cap_by_section 覆盖",
    "question_gap_mm": "由 room 档位覆盖",
    "page_bottom_breath_mm": "由 room 档位覆盖",
    "min_scale": "由 room 档位覆盖",
    "toc": "排版开关，跟书无关",
}

# when 判据的四档松紧：(名字, 页数下浮, 页数上浮, 题号密度 ±比例, 有题页比例 ±绝对值)
LEVELS = (
    ("宽", 0.35, 2.20, 0.60, 0.25),
    ("中", 0.50, 1.80, 0.40, 0.15),
    ("紧", 0.70, 1.40, 0.25, 0.10),
    ("最紧", 0.85, 1.15, 0.10, 0.05),
)


# ---------------------------------------------------------------- 读回这一次的结果

def _json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_run(out: Path, cfg_arg: str | None) -> dict:
    """从输出目录读回这一次的真实数字。一个文件缺失只降级，不中止——
    有人会把 crops/ ocr/ 清掉，但 probe/questions/accept 一般还在。"""
    probe = _json(out / "probe.json", {}) or {}
    accept = _json(out / "accept.json", {}) or {}
    book = _json(out / "book.json", {}) or {}
    qs = _json(out / "questions.json", []) or []

    cfg_path = None
    if cfg_arg:
        cfg_path = Path(cfg_arg)
    else:
        for cand in (out / "intake.yaml", out.parent / "intake.yaml",
                     out / "config.yaml", out.parent / "config.yaml"):
            if cand.is_file():
                cfg_path = cand
                break
    if cfg_path is None or not cfg_path.is_file():
        raise SystemExit(
            "找不到配置文件。这本用的配置不在输出目录里，用 --cfg 指过去：\n"
            "  python tools/learn.py <输出目录> --cfg <那次跑用的 intake.yaml>")

    cfg = common.load_config(str(cfg_path))

    src = probe.get("pdf") or ""
    if not src:
        raise SystemExit("probe.json 里没有源文件路径，用 --src 指明原书 PDF。")

    marks = 0
    narrow = 0
    qa = out / "qa.csv"
    if qa.is_file():
        with io.open(qa, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                flags = (row.get("flags") or "").strip()
                if flags and flags != "[]":
                    marks += 1
                    if "窄缝" in flags:
                        narrow += 1

    return {
        "out": out, "cfg_path": cfg_path, "cfg": cfg,
        "probe": probe, "accept": accept, "book": book,
        "src": src, "pages": probe.get("pages") or 0,
        "n": len(qs), "marks": marks, "narrow": narrow,
        "text_layer": probe.get("text_layer") or "none",
    }


# ---------------------------------------------------------------- 配置：带什么、不带什么

def carry_config(cfg: dict) -> tuple[dict, dict, list[str]]:
    """返回 (带走的配置, 几何量, 丢掉的键名)。几何量只写进注释，不进 config——
    它们必须和别的几何量一起改，单独抄一个就是把书切坏的最快方式。"""
    carry = {}
    for k in CARRY:
        if k in cfg and cfg[k] not in (None, "", [], {}):
            carry[k] = cfg[k]

    geometry = {}
    if isinstance(cfg.get("content_box"), list):
        geometry["content_box"] = cfg["content_box"]
    for k in ("strip_x0", "strip_x1", "strip_dpi", "content_top", "content_bottom_margin"):
        if isinstance(cfg.get("ocr"), dict) and k in cfg["ocr"]:
            geometry["ocr." + k] = cfg["ocr"][k]

    dropped = [k for k in cfg if k not in CARRY]
    return carry, geometry, dropped


# ---------------------------------------------------------------- when：松紧由回归网定

def _rng(v: float, margin: float, lo_cap=0.0, hi_cap=None) -> list:
    lo = round(max(lo_cap, v * (1 - margin)), 2)
    hi = round(v * (1 + margin), 2)
    if hi_cap is not None:
        hi = round(min(hi_cap, hi), 2)
    if hi <= lo:
        hi = round(lo + 0.05, 2)
    return [lo, hi]


def derive_when(fp: dict, level: tuple) -> dict:
    name, pg_lo, pg_hi, hit_m, ratio_m = level
    when = {"kind": fp["kind"], "two_col": bool(fp.get("two_col_pages"))}
    # 照片没有页数概念（一张一张拍），只认 kind —— 和 profiles/photo.yaml 一个口径
    if fp["kind"] == "pdf":
        n = int(fp["pages"])
        when["pages"] = [max(2, int(round(n * pg_lo))), max(3, int(round(n * pg_hi)))]
        when["text_layer"] = fp["text_layer"]
        when["hits_per_page"] = _rng(float(fp["hits_per_page"]), hit_m)
        when["pages_with_marks_ratio"] = _rng(
            float(fp["pages_with_marks_ratio"]), ratio_m, 0.0, 1.0)
    return when


def _baseline_fp(rec: dict, full: bool, cfg: dict) -> dict | None:
    """一本基线书的指纹。默认走便宜的近似（题数 ÷ 页数 + 文字层），
    full=True 才真的重扫一遍——7 本里有 106 页和 561 页的，那要一两分钟。"""
    pdf = Path(rec.get("pdf") or "")
    if not pdf.is_file():
        return None
    npages = int(rec.get("source_pages") or 0)
    if not npages:
        try:
            import pymupdf
            with pymupdf.open(str(pdf)) as d:
                npages = len(d)
        except Exception:
            return None
    if full:
        try:
            fp = classify.fingerprint(str(pdf), cfg)
            return fp
        except Exception:
            pass
    n = int(rec.get("n") or 0)
    return {
        "kind": "pdf", "text_layer": rec.get("text_layer") or "none",
        "pages": npages, "hits": n, "hits_per_page": round(n / max(npages, 1), 2),
        "images_per_page": 1.0, "two_col_pages": [],
        "num_chaptered_ratio": 0.0, "answer_density": 0.0,
        "pages_with_marks_ratio": 1.0, "chars_per_page": 0,
        "ink_rows_per_page": 0.0, "head_hits": {}, "title_lines": [], "scan": None,
    }


def baseline_conflicts(when: dict, own_src: str, cfg: dict, full=False) -> list[tuple[str, str]]:
    """哪些基线书会被这套判据误认。自己的书当然会命中，跳过。"""
    out = []
    own = str(Path(own_src).resolve()).lower()
    for jf in sorted(BASE.glob("*.json")):
        rec = _json(jf, {}) or {}
        if not rec.get("pdf"):
            continue
        if str(Path(rec["pdf"]).resolve()).lower() == own:
            continue
        fp = _baseline_fp(rec, full, cfg)
        if fp is None:
            out.append((jf.stem, "源文件不在，没法验（这是回归网的隐患，见 tools/regress）"))
            continue
        ok, bad = classify.check(fp, when)
        if ok and not bad:
            out.append((jf.stem, "；".join(ok)))
    return out


def choose_when(fp: dict, own_src: str, cfg: dict, full=False) -> tuple[dict, str, bool, list]:
    """从最宽一档开始试，第一档没有误认的就用它。四档都不行就用最紧那档并如实报出来——
    「一条都分不开」本身就是有用信息：说明判据维度不够，得看指纹里还有哪个量能分开。"""
    # 发布版里没有 tools/regress/（那目录存着真书的切点坐标）。没有基线时「宽」档
    # 没人能证伪，而宽档最容易把别的类型也认进来——宁可保守取中间档并说明原因。
    if not any(BASE.glob("*.json")):
        return derive_when(fp, LEVELS[1]), "中（没有回归基线可验，保守取中档）", False, []
    tried = []
    for lv in LEVELS:
        when = derive_when(fp, lv)
        bad = baseline_conflicts(when, own_src, cfg, full=full)
        tried.append((lv[0], when, bad))
        if not bad:
            return when, lv[0], False, tried
    return tried[-1][1], tried[-1][0], True, tried


# ---------------------------------------------------------------- 合并已有预案

def _union_rng(a, b) -> list:
    if not isinstance(a, list) or not isinstance(b, list):
        return b if b is not None else a
    return [min(a[0], b[0]), max(a[1], b[1])]


def merge_when(old: dict, new: dict) -> tuple[dict, list[str]]:
    """合并判据时**只放宽已有条件，绝不新增条件**。

    新增一条 = 把这类书的范围收窄。实例：bank-book.yaml 故意不写 text_layer，
    因为习题册既有扫描件也有带文本层的（它的 route 里就写着两条路）。如果合并时
    把「这本书是 none」加进去，下一本带文本层的习题册就认不出来了——
    而且不会报错，只是悄悄退回默认值。所以缺的键一律不补。
    """
    notes = []
    out = dict(old or {})
    for k, v in new.items():
        if k not in out:
            notes.append(f"when.{k} 原预案没有这一条，不补（补上会收窄范围）")
            continue
        if isinstance(v, bool):
            if bool(out[k]) != v:
                notes.append(f"when.{k} 原值 {out[k]} 与这次的 {v} 冲突，保留原值")
        elif isinstance(v, list) and isinstance(out[k], list):
            merged = _union_rng(out[k], v)
            if merged != list(out[k]):
                notes.append(f"when.{k} 放宽 {out[k]} → {merged}")
            out[k] = merged
        elif k == "text_layer":
            a = out[k] if isinstance(out[k], list) else [out[k]]
            b = v if isinstance(v, list) else [v]
            add = [x for x in b if x not in a]
            if add:
                notes.append(f"when.text_layer 增加 {add}")
            out[k] = a + add
    return out, notes



def merge_config(old: dict, new: dict) -> dict:
    out = dict(old or {})
    for k, v in new.items():
        if k == "question_patterns":
            out[k] = list(v)          # 题号正则由这类素材说了算，整条替换
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = list(out[k]) + [x for x in v if x not in out[k]]
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- 落盘

_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
# 这几个在 YAML 1.1 里会被读成布尔/空值，即使看着像普通单词也得加引号
_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "y", "n", "~"}


def _q(x) -> str:
    """一个标量怎么写。字符串优先不加引号（`kind: pdf` 比 `kind: "pdf"` 好读），
    只有在会被 YAML 误读时才套双引号——JSON 双引号形式正好是 YAML 的子集，
    正则里的反斜杠和引号都能正确转义（手抄预案时错的就是这一处）。"""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, str):
        if _BARE.fullmatch(x) and x.lower() not in _RESERVED:
            return x
        return json.dumps(x, ensure_ascii=False)
    return repr(x)


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*|[\u4e00-\u9fff]+", str(k)) else \
        json.dumps(str(k), ensure_ascii=False)


def _inline(v) -> str:
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_key(k)}: {_inline(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_inline(x) for x in v) + "]"
    return _q(v)


def _header_comments(p: Path) -> list[str]:
    """预案开头那段手写注释。合并时不能丢——那里写着「这一类是什么书」，
    是当初想清楚才写下来的，比自动生成的那几行有用得多。"""
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            out.append(line)
        elif line.strip() == "" and not out:
            continue
        else:
            break
    return out


def render(rec: dict, keep_header: list[str] | None = None) -> str:
    """手写 YAML 而不是 yaml.dump：这份文件是要人读、要人改的，
    现有预案的排版是「短列表写成 [20, 600]、坑一条一行」，dump 出来是两层缩进加短横线，
    跟邻居不一样。JSON 双引号标量在 YAML 里合法，所以正则不转义也不会坏。"""
    L = []
    if keep_header:
        L.extend(keep_header)
        L.append("#")
    L.append(f"# 自动生成：tools/learn.py，{rec['created']}")
    L.append(f"# 来源：{rec['src_name']}（{rec['pages']} 页 · {rec['n']} 题 · "
             f"文字层 {rec['text_layer']} · 成品 {rec['out_pages']} 页）")
    if rec["geometry"]:
        L.append("#")
        L.append("# 这本书的条带几何（**只对同系列书成立，不要照抄到别的书**）：")
        for k, v in rec["geometry"].items():
            L.append(f"#   {k}: {v}")
        L.append("#   改它们等于废掉整本 OCR 缓存；content_box 和 ocr.strip_x0 必须一起改。")
    L.append("")
    L.append(f"id: {_q(rec['id'])}")
    L.append(f"label: {_q(rec['label'])}")
    L.append(f"verified: {_q(rec['verified'])}")
    L.append(f"sample: {_q(rec['sample'])}")
    L.append(f"route: {_q(rec['route'])}")
    L.append(f"room: {rec.get('room') or 'standard'}")
    L.append("when:")
    for k, v in rec["when"].items():
        L.append(f"  {_key(k)}: {_inline(v)}")
    if rec.get("samples"):
        L.append("samples:")
        for s in rec["samples"]:
            L.append(f"  - {_q(s)}")
    if rec["config"]:
        L.append("config:")
        for k, v in rec["config"].items():
            L.append(f"  {_key(k)}: {_inline(v)}")
    if rec.get("expect"):
        L.append("expect:")
        for k, v in rec["expect"].items():
            L.append(f"  {_key(k)}: {_inline(v)}")
    if rec.get("accept"):
        L.append(f"accept: {_q(rec['accept'])}")
    if rec.get("pitfalls"):
        L.append("pitfalls:")
        for s in rec["pitfalls"]:
            L.append(f"  - {_q(s)}")
    return "\n".join(L).rstrip() + "\n"


def auto_pitfalls(rec: dict) -> list[str]:
    """只写「这次实测出来的、下本书会再遇到的」，最多两条。

    写进预案的每一句都该是踩过的。属于流程的知识（缓存怎么清、日志看哪行）
    留在 SKILL.md——塞进预案会让它变成第二本手册，而没人会去读第二本手册。"""
    out = []
    qa_note = f"{rec['n']} 题里 qa.csv 标了 {rec['marks']} 片"
    if rec["marks"]:
        out.append(qa_note + f"，其中窄缝 {rec['narrow']} 处。窄缝才要目检："
                   "切穿多是相邻两题共用一条边或跨页续文，不是错误")
    else:
        out.append(qa_note + "——切点全干净。这类版式可以「有标 0 片就直接交付」，一张图都不点")
    if rec["text_layer"] in ("none", "ocr"):
        out.append("条带几何是这类书的主要事故源：strip_x0 改了要整本重识别，"
                   "content_box 改了会切掉悬挂式题号半截（切点全对、qa 零标记，只能对着原书看），"
                   "两者必须一起改")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把一次做完的书沉淀成 profiles/*.yaml 预案",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("out", help="上一次跑完的输出目录（有 probe.json / questions.json 的那个）")
    ap.add_argument("--cfg", default="", help="那次跑用的配置文件（intake.yaml）")
    ap.add_argument("--src", default="", help="原书 PDF/照片；默认读 probe.json 里记的")
    ap.add_argument("--id", default="", help="新预案的 id（= 文件名，小写英文）。--merge 时不用给")
    ap.add_argument("--label", default="", help="给人看的一句话")
    ap.add_argument("--sample", default="", help="验证样本的描述，默认按实测数字拼")
    ap.add_argument("--route", default="", help="这类素材走哪条路线，默认按输入推")
    ap.add_argument("--room", default="", choices=["compact", "standard", "roomy"],
                    help="留白档位；不给就沿用原预案的")
    ap.add_argument("--pitfall", action="append", default=[],
                    help="额外记一条坑（可重复）。只写真的踩过的")
    ap.add_argument("--merge", default="", help="并进已有的预案 id，而不是新建")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的预案 / 忽略类型冲突")
    ap.add_argument("--check-full", action="store_true",
                    help="用完整指纹验回归网（准，但 7 本要一两分钟）")
    ap.add_argument("--no-baseline-check", action="store_true", help="跳过误认检查（不建议）")
    ap.add_argument("--dry", action="store_true", help="只打印，不写文件")
    a = ap.parse_args(argv)

    out = Path(a.out)
    if not out.is_dir():
        raise SystemExit(f"输出目录不存在：{out}")

    rec = load_run(out, a.cfg or None)
    src = a.src or rec["src"]
    if not Path(src).is_file():
        raise SystemExit(f"源文件不在了：{src}\n判据里的 pages/题号密度是按它算的，缺了没法沉淀。")

    print(f"== 读回 {out} ==")
    print(f"  {rec['pages']} 页 · {rec['n']} 题 · 文字层 {rec['text_layer']} · "
          f"qa 标记 {rec['marks']} 片")
    if not rec["accept"]:
        print("  ⚠ 没有 accept.json：这本没跑验收，verified 只能是 false")
    elif not rec["accept"].get("ok"):
        print(f"  ⚠ 验收没过：{rec['accept'].get('problems')}——先修完再沉淀，"
              "把一本有问题的书写成预案等于把问题固化成「这类书该这样」")

    # 指纹
    fp = classify.fingerprint(src, rec["cfg"])
    print(f"  指纹：每页 {fp['hits_per_page']} 个题号，有题页 "
          f"{fp['pages_with_marks_ratio']:.0%}，双栏 {len(fp['two_col_pages'])} 页")

    cfg_carry, geometry, dropped = carry_config(rec["cfg"])

    # 新建还是合并
    merge_target = None
    if a.merge:
        p = PROFILES / f"{a.merge}.yaml"
        if not p.is_file():
            raise SystemExit(f"没有预案 {a.merge}（{p} 不存在）。新建请给 --id")
        merge_target = p
        pid = a.merge
    else:
        hit = classify.classify(fp)["pick"]
        if hit and not a.force:
            raise SystemExit(
                f"这本已经命中现有预案「{hit['profile'].get('id')}」"
                f"（{'；'.join(hit['ok'])}）。\n"
                f"  同类书不要再立一个：两个 when 会互相抢，classify 只取条件最多的一条，"
                f"抢起来是隐性的、不报错。\n"
                f"  要把这次的调参结果沉淀进去，用：--merge {hit['profile'].get('id')}\n"
                f"  确实要另立一类，加 --force 并给一个能分开的 --id。")
        if not a.id:
            raise SystemExit(
                "新类型必须给 --id（文件名就是 id，用小写英文，例如 exam-paper-v2）。\n"
                "  先想清楚它跟已有预案的差别在哪——差别说不出来，说明该用 --merge。")
        pid = a.id
        if (PROFILES / f"{pid}.yaml").is_file() and not a.force:
            raise SystemExit(f"profiles/{pid}.yaml 已存在。要覆盖加 --force，要沉淀请用 --merge {pid}")

    # when
    if merge_target:
        old = classify.load_profiles()
        old = next((x for x in old if x.get("id") == pid), {}) or {}
        new_when = derive_when(fp, LEVELS[1])
        when, notes = merge_when(old.get("when") or {}, new_when)
        for x in notes:
            print("  ! " + x)
        level = "合并"
    else:
        if a.no_baseline_check:
            when = derive_when(fp, LEVELS[1])
            level, conflicted = "中（跳过误认检查）", False
        else:
            when, level, conflicted, tried = choose_when(fp, src, rec["cfg"], full=a.check_full)
            if not tried:
                print(f"  判据 {level}")
            for name, _, bad in tried:
                mark = "✓" if name == level else " "
                print(f"  {mark} 判据 {name}：" + ("无误认" if not bad else
                      "会误认 " + "、".join(b[0] for b in bad)))
            if conflicted:
                print("  ⚠ 四档都分不开。这不是松紧问题，是判据维度不够——"
                      "看指纹里还有哪个量能把它和那些书分开（章号占比？解答行占比？）")

    # 记录
    book_title = (rec["book"] or {}).get("title") or Path(src).stem
    out_pages = int((rec["accept"] or {}).get("pages") or 0)
    sample = a.sample or (f"{book_title} {rec['pages']} 页 {rec['n']} 题"
                          + ("（扫描件，走 OCR）" if rec["text_layer"] in ("none", "ocr") else "（文本层）"))
    route = a.route or (
        "scripts/segment_ocr.py（无文本层，靠条带 OCR 找题号）"
        if rec["text_layer"] in ("none", "ocr") else
        "scripts/segment.py（有文本层，直接读题号坐标）")

    pitfalls = list(a.pitfall)
    if merge_target:
        old_pit = next((x.get("pitfalls") or [] for x in classify.load_profiles()
                        if x.get("id") == pid), [])
        pitfalls = list(old_pit) + [x for x in auto_pitfalls(rec) if x not in old_pit] + pitfalls
    else:
        pitfalls = auto_pitfalls(rec) + pitfalls

    record = {
        "id": pid,
        "label": a.label or (next((x.get("label") for x in classify.load_profiles()
                                   if x.get("id") == pid), "") if merge_target else "")
        or f"未命名类型 {pid}（learn.py 自动生成，label 待补）",
        "verified": bool((rec["accept"] or {}).get("ok")),
        "sample": sample,
        "route": route,
        "room": a.room or "",
        "when": when,
        "config": cfg_carry,
        "expect": {"题数": rec["n"], "成品页数": out_pages} if rec["n"] else {},
        "accept": "",
        "pitfalls": pitfalls,
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "src_name": Path(src).name,
        "pages": rec["pages"],
        "n": rec["n"],
        "text_layer": rec["text_layer"],
        "out_pages": out_pages,
        "geometry": geometry,
        "samples": [],
    }

    if merge_target:
        old = next((x for x in classify.load_profiles() if x.get("id") == pid), {})
        record["samples"] = list(old.get("samples") or []) + [
            f"{book_title} {rec['pages']} 页 {rec['n']} 题"]
        record["config"] = merge_config(old.get("config") or {}, cfg_carry)
        record["expect"] = old.get("expect") or record["expect"]
        record["accept"] = old.get("accept") or ""
        record["room"] = a.room or old.get("room") or "standard"
        record["label"] = a.label or old.get("label") or record["label"]
        record["route"] = a.route or old.get("route") or route

    text = render(record, _header_comments(merge_target) if merge_target else None)

    # 汇总
    print()
    print("== 带走的配置 ==")
    if record["config"]:
        for k in record["config"]:
            v = record["config"][k]
            s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            print(f"  + {k}: {s[:80]}")
    else:
        print("  （没有可带走的项）")
    print("== 不带（这本书的账）==")
    shown = 0
    for k in dropped:
        if k in DROP_WHY:
            print(f"  - {k}：{DROP_WHY[k]}")
            shown += 1
    for k in dropped:
        if k not in DROP_WHY and shown < 14:
            print(f"  - {k}")
            shown += 1

    target = PROFILES / f"{pid}.yaml"
    print()
    if a.dry:
        print(f"== dry-run：会写到 {target} ==")
        print(text)
        return 0
    target.write_text(text, encoding="utf-8")
    print(f"已写入 {target}")
    print()
    print("下一步（不跑这三条，等于没沉淀）：")
    print(f'  python tools/selftest.py                                   # 预案会被 intake 读，自测必须全绿')
    print(f'  python scripts/classify.py "{src}" --why                    # 这本应该命中 {pid}，且不再报「没认出来」')
    print(f'  python scripts/intake.py "另一本同类书.pdf" --out 试跑 --defaults   # 回执里应出现 {pid}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
