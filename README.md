# pdf-to-workbook

把习题册 / 真题集的 PDF（或手机拍的书页）做成**能直接打印手写**的做题本：
每题切下来、下面配答题留白，加页眉、章节页、目录、书签。

![原书一页 vs 做题本一页](assets/preview.png)

> 左右都是真实页面，不是示意图。左边原书一页挤了 7 道题，题与题之间没有下笔的地方；
> 右边是同一批题，每道切下来、按题型配好答题区，页眉、章节、页码都是程序画的，
> 每张题图右下角标着「原书 P5」方便对照。

> 这个 README 只用于 GitHub 首页导航。**技能本体在 [`SKILL.md`](SKILL.md)**，
> 人类用户手册在 [`references/user-guide.md`](references/user-guide.md)。

## 安装

一句话：skill 就是「一个目录 + 里面一个 `SKILL.md`」。把整个仓库放进 agent 的技能目录就行。

### 1. 先决定放哪儿

| 范围 | 谁能用 | WorkBuddy / CodeBuddy | DeepSeek Harness（dsh） | Claude Code |
| --- | --- | --- | --- | --- |
| 用户级 | 本机所有项目 | `~/.workbuddy/skills/` | `~/.dsh/skills/`（也认 `~/.agents/skills/`） | `~/.claude/skills/` |
| 项目级 | 只有这个项目 | `<项目>/.workbuddy/skills/` | `<项目>/.dsh/skills/`（也认 `<项目>/.agents/skills/`） | `<项目>/.claude/skills/` |

dsh 会盯着这些目录，放进去即生效，**不用重启**；WorkBuddy 和 Claude Code 重启会话后可见。

### 2. 放进去

挑一行执行（以用户级为例）：

```bash
# WorkBuddy / CodeBuddy
git clone https://github.com/zhuliudan666/pdf-to-workbook.git ~/.workbuddy/skills/pdf-to-workbook

# DeepSeek Harness
git clone https://github.com/zhuliudan666/pdf-to-workbook.git ~/.dsh/skills/pdf-to-workbook

# Claude Code
git clone https://github.com/zhuliudan666/pdf-to-workbook.git ~/.claude/skills/pdf-to-workbook
```

Windows 的命令行里 `~/` 不管用，换成下面这行（把 `.dsh` 改成你要的目录）：

```
git clone https://github.com/zhuliudan666/pdf-to-workbook.git "%USERPROFILE%\.dsh\skills\pdf-to-workbook"
```

**不装 git 也行**：仓库页右上角 `Code → Download ZIP`，解压出一个目录，丢进上表的任一位置。目录名随便改，但层级必须是 `skills/pdf-to-workbook/SKILL.md`——多套一层就认不出来。

### 3. 装依赖（只装一次）

把 `<技能目录>` 换成你在第 1 步选的那个（例如 `~/.workbuddy/skills`）：

```bash
python -m pip install -r <技能目录>/pdf-to-workbook/requirements.txt
```

**用 `python -m pip` 而不是 `pip`**：Windows 上经常同时装着两个 Python，`pip` 和 `python`
不是同一个，装完了 `import` 还是报 `ModuleNotFoundError`。`python -m pip` 保证装进当前这个解释器。

只处理带文本层的 PDF 时，`rapidocr-onnxruntime` 和 `opencv-python` 可以不装（那是扫描件和手机拍照路线要的）。另外需要一个中文字体，见[依赖与字体](#依赖与字体)。

### 4. 验证

```bash
# 文本层路线要的四个：
python -c "import pymupdf, yaml, numpy, PIL; print('文本层路线：依赖齐了')"

# 扫描件 / 手机拍照另外还要这两个（不跑这两条路线可以跳过）：
python -c "import cv2, rapidocr_onnxruntime; print('扫描件路线：依赖齐了')"
```

一条都不报错就说明装好了。报了 `ModuleNotFoundError` 但 pip 说已安装，就是第 3 步那两个
`pip` / `python` 不是同一个——回头用 `python -m pip` 重装。

- **dsh**：输入 `/pdf-to-workbook`，回话说技能已加载就成了
- **WorkBuddy / Claude Code**：问一句「pdf-to-workbook 这个技能现在能用吗」，或者直接说「把这本书做成做题本」
- **没出现**：九成是目录多套了一层，检查有没有 `skills/pdf-to-workbook/pdf-to-workbook/SKILL.md` 这种结构

> 本仓库是私有的，克隆前要先登录 GitHub（`gh auth login`，或用 GitHub Desktop 打开一次）。
> 想让别人免登录安装，把仓库可见性改成 public。

### 5. 也可以让 agent 自己装

能。只要那个 agent 能跑命令、能写文件，把下面这段整个丢给它就行——不用你自己敲 shell：

```text
把这个仓库装成你的技能，装完自己验证一遍：
https://github.com/zhuliudan666/pdf-to-workbook

1. 放进你的用户级技能目录（WorkBuddy/CodeBuddy 是 ~/.workbuddy/skills/，
   DeepSeek Harness 是 ~/.dsh/skills/，Claude Code 是 ~/.claude/skills/），
   层级必须是 <技能目录>/pdf-to-workbook/SKILL.md，不要在里面再套一层同名目录
2. 装依赖：pip install -r requirements.txt
3. 自检：python -c "import pymupdf, yaml, numpy, PIL" 不报错就算齐了；再跑
   python scripts/intake.py --help 能打出用法，说明脚本本身也起得来
4. 告我装到哪个路径了、现在能不能用
```

它做完这四步就等于装好了。要是它只 clone 了没装依赖，你补一句「把 requirements.txt 装上」即可。

> 前提是本仓库对它有读权限：私有仓库需要它手上已经有你的 GitHub 凭据
> （桌面端登录过、或 `gh auth login` 过）。都没有的话，把仓库改成 public 最省事。

## 怎么用

一句话：**把 PDF 丢给它，说「做成做题本」**。它先问你两三句，然后出两个 PDF。

```mermaid
flowchart LR
  A[习题册.pdf] --> B["intake：读文件<br/>出计划、问你 2~3 句"]
  B --> C{你看一眼计划}
  C -->|哪里不对| B
  C -->|可以| D["build：切题 → 裁图<br/>→ 质检 → 排版 → 验收"]
  D --> E[做题本.pdf<br/>打印用]
  D --> F[校对单.pdf<br/>五分钟翻一遍]
```

### 一次真实的对话

```
你：把 李林四套卷.pdf 做成做题本，我要打印

它：16 页，有文本层。目录里认出 4 套合订，已按册分章；题号正则命中 88 题，
    成品约 29 页。每页放 选填 4 道 / 大题 3 道，A4 单面。这样跑？

你：（看一眼，没问题）行

它：跑完了。做题本 29 页 / 88 题；校对单 9 页，重点 0 条、关注 26 条。
```

三个地方值得留意：

1. **它先出计划再动手**。范围、题量、每页几道、纸张都在里面；想改就直说
   （「留白宽一点」「只做前 3 页」「打双面」），它认这些说法。
2. **它会先试前几页**（你说「先跑3页」）。切点对了再放开整本，别一上来等十分钟。
3. **校对单要翻**。做题本里每道题的图是切下来的，切歪一两道肉眼能看出来；
   校对单把可疑的按「重点 / 关注 / 抽检」排好，翻一遍就知道要不要重跑。

### 不想让 agent 代劳

按 `SKILL.md` 开头的「工作流程」手敲。`build.py` 一条就跑完八步（体检→切题→裁图→
质检→结构校验→校对单→拼版→验收），但**它只保证「切得下去」，不保证「没切进内容」**——
所以后面还要补一次自检和诊断：

```bash
pip install -r requirements.txt
cd /path/to/pdf-to-workbook && SKILL="$PWD" && PY=python

# ① 出计划（问你 2~3 句，也可以 --say 直接把原话传进去）
"$PY" "$SKILL/scripts/intake.py" 书.pdf --out 输出目录 --say "用户原话"

# ② 一键全流程
"$PY" "$SKILL/scripts/build.py"  书.pdf 输出目录/intake.yaml 输出目录

# ③ 补一次自检：查「有没有切进内容」，build 兜不住这一层
"$PY" "$SKILL/scripts/selfcheck.py" 书.pdf 输出目录 输出目录/intake.yaml --json 输出目录/selfcheck.json
"$PY" "$SKILL/scripts/diagnose.py" --check 输出目录/selfcheck.json --questions 输出目录/questions.json
```

产物：

| 文件 | 用途 |
| --- | --- |
| `输出目录/做题本.pdf` | 打印用 |
| `输出目录/校对单.pdf` | 五分钟翻一遍：红框必看、黄框快扫、灰框查缺题 |
| `输出目录/联系表.png` | 全部题图的缩略图，一眼看切点齐不齐 |
| `输出目录/selfcheck.json` | 第 ③ 步的自检结果，诊断读它 |

`diagnose.py` 说『全干净』才算收工。它报红时按 `SKILL.md` 的「症状 → 参数」表改，
不要凭感觉调阈值。

## 第一本慢，同类的第二本快

**慢的不是跑，是调参。**

跑本身按页数线性耗时：切题、裁图、拼版，扫描件再加 OCR（约 5 秒/页）。OCR 缓存是按书
存的（缓存名带页码），换书不共享——所以第二本该等多久还是等多久，这部分不会变快。

省下来的是**摸配置**：切点压没压准、页眉水印要不要剔、多套合订怎么分章、每页放几道。
这些沉淀在两个地方：

| 机制 | 作用 |
| --- | --- |
| `profiles/` 六套预案 | `classify.py` 认出素材类型就自动套配置，不用逐项调 |
| `tools/learn.py` | 把这次跑通的配置固化成新预案，下次同类书直接命中 |

前提是**版式同源**——同出版社、同系列、同一批扫描件。第二本往往一次就过；
换一家出版社，题号写法、页眉位置、栏数都变，预案不命中，等于重新来一遍。

所以这套东西的成本随「书型」递减，不随「本数」递减。

> 心理预期：第一次做某一类书，留出一两轮调参的时间（改的是配置，不是代码）；
> 同类第二本开始，多数能一轮到底。`profiles/` 里目前 `handout`（讲义课后题）标着
> `verified: false`，第一次拿真讲义进来要格外看一眼校对单。

## 适合什么素材

| 素材 | 怎么描述它 | 实测成品 |
| --- | --- | --- |
| 整本练习册 / 题库 | 一题一号，全书都是题 | 106 页 → 720 题 / 220 页 |
| 套卷 / 真题合订 | 一张卷子二十来道，按「一、选择题」「二、填空题」分段；多套合订会按册自动分章 | 16 页 / 4 套 → 88 题 / 29 页 |
| 单章 / 单讲节选 | 一本厚书里只要第 18 讲 | 5 页 → 15 题 |
| 讲义章末课后题 | 正文占九成，题只在每章最后一页 | 45 页 → 173 题 / 70 页 |
| 讲义里的例题 | 「例 1.2」后面紧跟「解：」或「【详解】」 | 69 例 / 6 章 |
| 手机拍的书页 | 一页一张照片 | 逐页给题号，直接出 PDF |

**做不了**：双栏排版（会把两栏当一行，36 题只认出 18 道）、没有题号的素材、加密 PDF。

> 页眉印着「公众号：xxx」这类引流行不碍事，会被剔掉，不会印进题图。

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
