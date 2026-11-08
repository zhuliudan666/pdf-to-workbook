# pdf-to-workbook

把习题册 / 真题集的 PDF（或手机拍的书页）做成**能直接打印手写**的做题本：
每题切下来、下面配答题留白，加页眉、章节页、目录、书签。

![成品示例](assets/preview.png)

> 上图不是示意图，是拿一份自拟样本册跑完整条流水线的真实输出：题干是原书的位图裁切，
> 留白、页眉、页码是程序画的。

> 这个 README 只用于 GitHub 首页导航。**技能本体在 [`SKILL.md`](SKILL.md)**，
> 人类用户手册在 [`references/user-guide.md`](references/user-guide.md)。

## 安装

skill 就是「一个目录里放一个 `SKILL.md`」。把这个仓库放到 agent 的技能目录，重启后即可用。

**WorkBuddy / CodeBuddy**（用户级，所有项目可见）：

```bash
git clone https://github.com/zhuliudan666/pdf-to-workbook.git ~/.workbuddy/skills/pdf-to-workbook
```

```powershell
# Windows PowerShell
git clone https://github.com/zhuliudan666/pdf-to-workbook.git "$env:USERPROFILE\.workbuddy\skills\pdf-to-workbook"
```

只想在某个项目里用，就放到 `<项目根>/.workbuddy/skills/pdf-to-workbook`。

**Claude Code**：放 `~/.claude/skills/pdf-to-workbook`（个人）或 `<项目根>/.claude/skills/pdf-to-workbook`（项目）。

**不想用 git**：右上角 `Code → Download ZIP`，解压出一个目录，丢进上面任一位置。目录名可以改，但里面必须有 `SKILL.md`。

> 本仓库是私有的，克隆前要先登录 GitHub（`gh auth login`，或用 GitHub Desktop 打开一次）。
> 需要免登录分发的话，把仓库可见性改成 public。

**验证装好了没**：问 agent「pdf-to-workbook 这个技能现在能用吗」，或直接说「把这本书做成做题本」。
技能列表里没出现，多半是目录层级多了一层（`skills/pdf-to-workbook/pdf-to-workbook/SKILL.md` 这种）。

装完还差一步依赖，见[依赖与字体](#依赖与字体)。

## 三句话

```
book.pdf ─① intake 出计划─→ 问你 2~3 句 ─→ intake.yaml
                                            │
            ② build 8 步全自动 ────────────┘
               ↓
         做题本.pdf（打印） + 校对单.pdf（五分钟翻一遍）
```

## 能做什么

| 素材 | 怎么描述它 | 实测成品 |
| --- | --- | --- |
| 整本练习册 / 题库 | 一题一号，全书都是题 | 106 页 → 720 题 / 220 页 |
| 套卷 / 真题合订 | 一张卷子二十来道，按「一、选择题」「二、填空题」分段 | 52 页 → 385 题 / 120 页 |
| 单章 / 单讲节选 | 一本厚书里只要第 18 讲 | 5 页 → 15 题 |
| 讲义章末课后题 | 正文占九成，题只在每章最后一页 | 45 页 → 173 题 / 70 页 |
| 讲义里的例题 | 「例 1.2」后面紧跟「解：」或「【详解】」 | 69 例 / 6 章 |
| 手机拍的书页 | 一页一张照片 | 逐页给题号，直接出 PDF |

**做不了**：双栏排版（会把两栏当一行，36 题只认出 18 道）、没有题号的素材、加密 PDF。

## 怎么用

在支持 skill 的 agent 里说一句「把这本书做成做题本」，或者按 `SKILL.md` 开头的
「工作流程」手敲三条命令：

```bash
pip install -r requirements.txt
cd /path/to/pdf-to-workbook && SKILL="$PWD" && PY=python

"$PY" "$SKILL/scripts/intake.py" 书.pdf --out 输出目录 --say "用户原话"
"$PY" "$SKILL/scripts/build.py"  书.pdf 输出目录/intake.yaml 输出目录
```

产物是 `输出目录/做题本.pdf` 和 `输出目录/校对单.pdf`。

## 目录

| 路径 | 是什么 |
| --- | --- |
| `SKILL.md` | 技能本体。前面是工作流程，`# 以下给维护者` 之后是判据与设计取舍 |
| `references/user-guide.md` | 用户手册：三步命令、能不能做、档位、qa.csv 怎么读、症状表 |
| `references/transcribe-prompt.md` | 矢量重排路线的转写提示词 |
| `references/example-items.json` | 矢量重排的输入格式样例（4 道题，含 `uncertain` 字段怎么写） |
| `scripts/` | 流水线：intake → probe → 切题 → 裁图 → 质检 → 校对单 → 拼版 → 验收 |
| `profiles/` | 6 类素材预案，命中后自动套配置 |
| `tools/` | 沉淀预案（learn）、清理中途产物（clean）、PDF 转图（render_preview） |
| `assets/preview.png` | README 顶部那张成品图 |
| `config.yaml` | 全部可调参数 |

> `tools/learn.py` 的「试档」功能要读 `tools/regress/` 里的回归基线，那目录存着真书的切点坐标，
> 没有随发布版提供。缺基线时它只写配置、不做试档，不会报错。

## 依赖与字体

见 `requirements.txt`。另外需要**一个中文字体**（页眉、章节标题、目录是我们自己画的字）：
Windows 自带微软雅黑、macOS 自带苹方、Linux 装 `fonts-noto-cjk`。
一条都找不到时会明确警告，不会静默出一份没有标题的成品。

## 已知限制

- 双栏排版不支持，体检阶段会拦下
- 文本层路线没有「页首章节标题不归第一题」这道闸（详见 `SKILL.md` 的「已知限制」）
- 扫描件整本 OCR 约 5 秒/页，有缓存，重跑只处理新增页

## 许可

[MIT](LICENSE)。自己做的书自己用，出问题别找我。
