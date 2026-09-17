# claude-agent-team

[![test](https://github.com/another-bbzj/claude-agent-team/actions/workflows/test.yml/badge.svg)](https://github.com/another-bbzj/claude-agent-team/actions/workflows/test.yml) ![python](https://img.shields.io/badge/python-3.8%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

**让 Claude Code 像一家小公司一样干活**：一支常驻的、分部门的 Agent 团队（每人有名字、职责、模型档位、动画形象），一套成员之间的办公协议（工单 / 收件箱 / 交接单），外加一块浏览器里的**实时指挥室看板**——谁在干什么、谁给谁发了消息、花了多少 token 和钱，一眼看清。

```
你：用 agent team 做一个记单词的网页应用

队长（主会话）：定规模 → 写 SPEC 契约 → 拆工单
   ├─ 🛠 研发部   阿服 写数据层      小界 写界面        （并行）
   ├─ 🔍 质量部   测测 写测试        老审 联调审查      （第二波）
   ├─ 📄 文档部   文文 写 README
   └─ 队长亲自跑测试、走流程、验收，向你汇报每个人的结论 + 本次成本
```

> 适用：Claude Code（桌面版 / CLI）。零依赖，只需 Python 3.8+。Windows / macOS / Linux。

![看板总览](docs/img/01-overview.png)

📖 **图文操作手册：[docs/GUIDE.md](docs/GUIDE.md)**（每个面板、每个按钮、每个字段都有截图和说明）

---

## 目录

1. [3 分钟装好](#1-3-分钟装好)
2. [第一次使用：完整走一遍](#2-第一次使用完整走一遍)
3. [看板怎么看](#3-看板怎么看)
4. [团队是怎么协作的](#4-团队是怎么协作的)
5. [内置成员与模型分级](#5-内置成员与模型分级)
6. [添加你自己的部门和成员](#6-添加你自己的部门和成员)
7. [省 token 的规则](#7-省-token-的规则)
8. [自检与测试](#8-自检与测试)
9. [常见问题](#9-常见问题)
10. [目录结构 / 形象素材 / 致谢](#10-目录结构)

---

## 1. 3 分钟装好

```bash
git clone https://github.com/another-bbzj/claude-agent-team.git
cd claude-agent-team
python install.py
```

`install.py` 做的事（全部可用 `python install.py --uninstall` 撤销）：

| 装到哪 | 内容 |
|---|---|
| `~/.claude/agents/` | 8 位常驻成员（Claude Code 原生 subagent 格式） |
| `~/.claude/skills/agent-team/` | 团队协议技能 |
| `~/.claude/team-board/` | 看板服务 |
| `~/.claude/CLAUDE.md` | 追加一段全局约定（用标记包裹，不动你原有内容） |
| `~/.claude/settings.json` | 追加一个 SessionStart 钩子，让看板随会话自动常驻（原文件先备份） |

装完**新开一个 Claude Code 会话**（任何项目文件夹都行）。看板会自动在后台启动：打开 http://127.0.0.1:7788/ 。想立刻看：`python ~/.claude/team-board/ensure.py`。

如果你装了 Codex 桌面版，安装脚本还会从你本机提取 9 只 Codex 宠物形象（见[形象素材](#形象素材)）。

---

## 2. 第一次使用：完整走一遍

**准备**：在 Claude Code 里打开任意项目文件夹（空文件夹也行），看板页面在旁边开着。

**第 1 步 · 下达任务。** 在对话里说：

> 用 agent team 做一个纯前端的闪卡记单词应用：卡组管理、翻牌复习、间隔重复算法、CSV 导入导出。

关键词「用 agent team」会触发 `agent-team` 技能。你也可以说「团队」「多代理」「分头做」「并行开发」。

**第 2 步 · 队长定规模。** Claude（队长）会先判断：

- **S 级**（≤ 2 个文件）→ 不开团队，直接做，并告诉你原因。别为小事开会。
- **M 级**（3–6 个模块）→ 队长自己写契约、拆工单。
- **L 级**（> 6 个模块或陌生领域）→ 先派 `team-architect`（阿图）出契约和工单。

**第 3 步 · 建办公室。** 队长在项目里创建 `.team/`：

```
.team/
  SPEC.md           契约：目录结构、每个模块的导出签名、数据模型、DOM/class 约定、测试命令
  tickets/01-*.md   工单：一张一文件，写清 owner / 改哪些文件 / 依赖谁 / 验收标准
  inbox/<成员>.md    每位成员一个收件箱
  handoffs/         成员完成时写的交接单
```

**第 4 步 · 派工。** 队长把没有依赖的工单同时派出去（后台并行）。看板上你会看到：队长头顶飞出「派工」小球落到成员身上，成员开始奔跑动画，卡片上显示它此刻在调用什么工具。

**第 5 步 · 成员交流。** 阿服写完数据层，把接口签名写进 `inbox/frontend-dev.md`；小界开工先读收件箱。看板上画成一条「留言」飞线。

**第 6 步 · 门禁。** 第一波交付后，队长按规则派第二波：≥ 2 人写了代码 → 必须派测测（测试）+ 老审（联调审查）；交付给人用 → 必须派文文（文档）。看板头部的「部门 x/y」变红闪烁就是提醒有必需部门缺席。

**第 7 步 · 验收汇报。** 全部交付后，队长**亲自**跑测试、走主流程，然后向你汇报：每个成员一两句结论、并行开发暴露的接缝问题、未修的隐患、本次成本（从看板读）。

整个过程你什么都不用操作，看着看板就行；想插手时直接在对话里说（例如「让老审再看一遍 store.js」）。

---

## 3. 看板怎么看

![部门汇总与成本](docs/img/02-stats.png)

打开 http://127.0.0.1:7788/ ，自上而下（更详细的逐项说明见 [docs/GUIDE.md](docs/GUIDE.md)）：

| 区域 | 内容 |
|---|---|
| **顶栏** | 阶段（组队中 / 协调中 / 收尾完成）· 进行中 / 已完成人数 · 部门完整性 · 模型构成 · 估算成本 · 全队 token 合计与缓存命中率 · 会话选择 · 主题 |
| **LIVE 条** | 实时监听状态、项目名、当前指令 |
| **FORMATION 舞台** | 队长在上，各部门面板分组；每位成员：动画形象、名牌、模型·强度、成本、当前工具、进度条、用时。飞线：派工 / 回报 / 留言 / 交接 / 转达。「▶ 回放通信」可回看本次全部交互 |
| **DEPARTMENTS 部门汇总** | 每个部门的人数、进行/完成/失败、工具次数、tokens、缓存命中、累计用时、模型、成本；下方是部门间的成本 / tokens / 用时对比条 |
| **COST 成本排行** | 成员按估算成本排序，颜色区分 opus / sonnet / haiku |
| **TASKS 任务卡片** | 每张工单的负责人、状态、交付摘要 |
| **COMMS 通信流** | 派工、回报、留言、交接、转达，带正文 |
| **ACTIVITY 动作流** | 所有成员的工具调用时间线 |
| **ROSTER 常驻成员** | 所有成员定义；＋ 新建成员、⚙ 部门管理 |

点任何成员打开详情抽屉：任务指令、模型、token 明细（输入/输出/缓存读/缓存写/命中率）、写过的文件、通信记录、交付回报、完整动作时间线。

动画含义：读文件/搜索 = 审视姿势；写代码/跑命令 = 奔跑；交付 = 跳跃；停滞 = 打瞌睡 zZ；出错 = 倒地。

看板**只读**你本机 `~/.claude/projects/**/subagents/agent-*.jsonl` 转录文件——不需要成员做任何登记，不调用任何 API，不联网。

---

## 4. 团队是怎么协作的

Claude Code 桌面版的子代理之间没有"私信"工具，所以团队像真实办公室一样靠**文件**交流，规则在 `skills/agent-team/office.md`：

- **工单**是一条竖切片（穿过所需的每一层，单独可演示），大小能装进一个新上下文窗口；`files` 互不重叠，声明 `blocked_by`。
- **收件箱**追加式留言：接口签名、路径:行号、需要对方做的事。收件人开工第一步读它。
- **交接单** ≤ 25 行：改了什么、导出了什么、与契约的偏差、验证过什么、给谁留了言、遗留风险。
- **队长**只读交接单的三个字段（偏差 / 风险 / 留言），需要裁决的写进 `decisions.md`，阻塞解除的工单立即派出。

看板把这些文件动作画成飞线：写别人收件箱 = 留言，读别人写的文件 = 交接，派工指令里点名"某某已交付…" = 转达。

---

## 5. 内置成员与模型分级

| 部门 | 成员 | `subagent_type` | 模型 · 强度 | 何时出场 |
|---|---|---|---|---|
| 🎯 指挥部 | 阿图·架构师 | `team-architect` | opus · high | L 级任务：出契约与工单 |
| 🛠 研发部 | 阿服·后端 / 小界·前端 | `backend-dev` / `frontend-dev` | sonnet · medium | 任何写代码的任务 |
| 🔍 质量部 | 测测·测试 / 老审·审查联调 | `qa-tester` / `code-reviewer` | sonnet · medium / opus · high | ≥ 2 人写了代码时必到 |
| 🔭 研究部 | 探探·研究分析 | `researcher` | haiku · low | 调研、读外部代码、查文档 |
| 📄 文档部 | 文文·文档 | `docs-writer` | haiku · low | 交付给人用时 |
| 🚀 运维部 | 发发·发布 | `release-ops` | sonnet · low | 构建、打包、CI、部署脚本 |

**为什么不全用最强模型**：调研和写文档用 haiku 便宜 10 倍以上；写代码 sonnet 够用；只有架构拆解和联调审查这种需要深度推理的活才上 opus。每个成员的档位写在它的 `.md` 里，改一行就换档；派工时也可以临时覆盖（`model` 参数）。

你也可以不开团队、直接点名：「让 code-reviewer 看看我刚改的这几个文件」——Claude 会按 `description` 自动委派。

---

## 6. 添加你自己的部门和成员

内置成员只覆盖通用软件开发。你的领域（嵌入式、数据科学、游戏、法务……）这样加：

![部门管理](docs/img/05-dept.png)

**加部门**：看板底部 ROSTER 面板 → **⚙ 部门管理** → ＋ 新增部门。填 id（如 `embedded`）、名称、图标、出场规则：

| 规则 | 含义 |
|---|---|
| `optional` | 可选 |
| `code` | 只要有人写代码就需要 |
| `code2` | ≥ 2 人写代码才需要（质量部用这个） |
| `deliver` | 交付给人用时需要（文档部用这个） |
| `always` | 总是需要 |

![新建成员](docs/img/04-member.png)

**加成员**：**＋ 新建成员** → 表单顶部有实时预览。字段：

- **标识**（`subagent_type`，小写字母/数字/连字符）、显示名、岗位、部门
- **模型**（sonnet / opus / haiku / fable / inherit）、**思考强度**（low / medium / high / xhigh / max）
- **形象**、颜色
- **简介**：写清"什么时候该用它"——Claude 靠这句自动委派，越具体越准
- **预载技能**：`~/.claude/skills/` 里的技能名，逗号分隔（把你领域的 SKILL.md 挂上去，成员开工时自动带着）
- **系统提示词**：成员的工作方法、硬性规范、验证方式

保存即写入 `~/.claude/agents/<标识>.md`（Claude Code 原生格式）。**新开的会话**才会加载新成员。点成员卡片可编辑，悬停右侧 ✕ 可删除。

也可以直接手写 `~/.claude/agents/*.md`，看板会自动读到。示例：

```markdown
---
name: firmware-dev
description: 固件工程师（嵌入式部）。写或改 MCU 固件、外设驱动、中断与主循环划分时使用。
model: sonnet
effort: medium
memory: user
skills:
  - my-mcu-skill
---
你是嵌入式部的固件工程师。开工先读 .team/SPEC.md、自己的工单和收件箱……
```

---

## 7. 省 token 的规则

写在 `skills/agent-team/lean.md`，成员和队长都遵守：

- **队长窗口最贵**：派工只给路径指针（契约、工单、收件箱），不粘内容；成员汇报 ≤ 200 字；队长只读交接单的三个字段。
- **成员不通读全库**：按工单 `files` 定向 grep。
- **写代码用"懒惰资深"阶梯**：需要存在吗 → 库里已有？→ 标准库？→ 平台原生？→ 已装依赖？→ 能一行？→ 才写最少的新代码。不做单实现的接口、永不变化的配置、"以后用"的脚手架。
- **结算**：验收时看板 `totals` 给出成员成本与模型构成；某个成员成本明显高于同档同事，查它的交接单，通常是通读全库或反复重试。

---

## 8. 自检与测试

```bash
python -m unittest discover -s tests -v
```

11 个测试，只用标准库，1 秒跑完：用合成的 Claude Code 转录跑完整解析（成员状态 / 模型 / token / 成本 / 派工 / 回报 / 留言 / 交接 / 转达 / 部门门禁），成员与部门的增删改校验，HTTP 接口，以及 `install.py` 在临时 HOME 上的安装 / 重复安装 / 卸载。GitHub Actions 在 Windows / macOS / Linux × Python 3.8 / 3.12 上自动跑。

## 9. 常见问题

**看板打不开 / 页面空白**
新开一个会话让钩子触发，或手动 `python ~/.claude/team-board/ensure.py`。端口被占用可改 `TEAM_BOARD_PORT` 环境变量。

**看板显示"还没有会话派出过子代理"**
正常——你还没派过团队。派一次后自动出现；右上角下拉可切换到任何历史会话回看。

**新建的成员派不出去**
成员在会话启动时加载，新开一个会话即可。

**Claude 没开团队、自己做了**
它判断为 S 级（≤ 2 个文件）。明确说「就用团队做」它会照办。

**成员之间为什么不直接聊天**
桌面版子代理没有私信工具，所以走 `.team/inbox/` 文件；效果一样，看板照样画出来。

**成本数字准吗**
按 `team-board/team.json` 里的公开单价估算，包含缓存读写；不是账单。

**换电脑**
把仓库 clone 过去再 `python install.py`；钩子路径按新机器自动生成。

---

## 10. 目录结构

```
tests/             自动化测试（python -m unittest discover -s tests）
docs/              图文操作手册 GUIDE.md 与截图
team-board/        看板：team_board.py（标准库 HTTP + 转录解析）、index.html（单文件前端）、
                   ensure.py（钩子入口）、team.json（部门/价格配置）、import_codex_pets.py（从本机 Codex 提取宠物）
agents/            8 位通用常驻成员定义
skills/agent-team/ 团队协议：SKILL.md 步骤、roles.md 部门与模型档位、office.md 办公目录规范、lean.md 省 token 规则
CLAUDE.global.md   写入 ~/.claude/CLAUDE.md 的全局约定
install.py         安装 / 卸载
```

### 形象素材

仓库自带 **13 只**：4 只机器人来自 [cc-haha](https://github.com/NanmiCoder/cc-haha)（MIT），9 只由 `team-board/make_pets.py` 程序化绘制（pip / cubo / drip / mush / kit / spark / bolt / puff / tank，随仓库 MIT 发布，想改配色或加新形象改脚本重跑即可）。每个岗位各占一只，开箱即不重复。

**可选：再加 9 只 Codex 宠物**（Codex、Dewey、Fireball、Hoots、Rocky、Seedy、Stacky、BSOD、Null Signal）：它们是 OpenAI Codex 桌面客户端里的素材，版权归 OpenAI，仓库不附带；如果你装了 Codex，`install.py` 会自动从**你本机**的安装文件里提取，或手动运行：

```bash
python ~/.claude/team-board/import_codex_pets.py    # 自动查找；也可传 app.asar 路径
```

提取结果只留在你本机；装了之后总共 22 只可选。看板会自动使用 `sprites/` 下所有 8 列 × 11 行、192×208/帧 的 `.webp` 雪碧图（行序：idle / 跑右 / 跑左 / 挥手 / 跳 / 失败 / 等待 / 工作 / 审阅），放进更多就有更多形象。

### 致谢

思路借鉴 [cc-haha](https://github.com/NanmiCoder/cc-haha) 的 Agent Teams 工作台；token 规则借鉴 [ponytail](https://github.com/DietrichGebert/ponytail) 与 [mattpocock/skills](https://github.com/mattpocock/skills)。

MIT License
