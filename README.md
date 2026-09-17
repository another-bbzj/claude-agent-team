# claude-agent-team

给 Claude Code 装一支**部门制的常驻 Agent 团队**，外加一块**实时指挥室看板**：每位成员有固定的名字、职责、模型档位和动画形象；成员之间靠项目里的 `.team/` 办公目录交流；队长（主会话）按技能里的门禁派工、收报、验收。

```
┌─ 指挥部 ───────────────────────────────────────────┐
│  队长（主会话）      阿图·架构师  team-architect  opus/high │
├─ 研发部 ───────────────────┬─ 质量部 ─────────────────────┤
│ 阿服 backend-dev  sonnet/medium │ 测测 qa-tester    sonnet/medium │
│ 小界 frontend-dev sonnet/medium │ 老审 code-reviewer  opus/high  │
├─ 研究部 ──────────┬─ 文档部 ─────────┬─ 运维部 ─────────────┤
│ 探探 researcher   │ 文文 docs-writer  │ 发发 release-ops     │
│   haiku/low       │   haiku/low       │   sonnet/low         │
└───────────────────┴───────────────────┴──────────────────────┘
  + 你自己的部门与成员（看板里「部门管理」「＋ 新建成员」）
```

## 安装

需要 Python 3.8+，无第三方依赖。

```bash
git clone https://github.com/another-bbzj/claude-agent-team.git
cd claude-agent-team
python install.py
```

安装后新开一个 Claude Code 会话（任何项目文件夹都行），看板会由 SessionStart 钩子自动常驻在 http://127.0.0.1:7788/ 。想立刻看：`python ~/.claude/team-board/ensure.py`。

卸载：`python install.py --uninstall`（只移除本工具加入的内容，`settings.json` 原文件有备份）。

## 怎么用

在对话里说「用 agent team 做 ×××」，Claude 会先调用 `agent-team` 技能：

1. **定规模** S/M/L —— S 级（≤2 个文件）不开 team，直接做。
2. **建办公室** —— 在项目里生成 `.team/`：`SPEC.md` 契约、`tickets/` 工单（竖切片 + `blocked_by`）、每人一个 `inbox/`。
3. **派工** —— `subagent_type` 点名常驻成员，prompt 只给路径指针，后台并行。
4. **收报与转达** —— 成员写交接单、给同事收件箱留言；汇报 ≤ 200 字。
5. **门禁** —— ≥2 人写了代码必须过 `qa-tester` + `code-reviewer`；交付给人用必须有 `docs-writer`；缺席不得宣布完成。
6. **验收** —— 队长亲自跑测试、走主流程，并在看板 `totals` 里核对成本与模型构成。

也可以直接点名：「让 control-engineer 看看这个速度环为什么振荡」。成员定义在 `~/.claude/agents/*.md`，靠 `description` 自动委派；改 `model:` / `effort:` 即可换档，加一个 `.md` 就多一位成员。

## 看板能看到什么

- **部门与队形**：队长在上，各部门面板分组，必需部门缺席时红色闪烁。
- **每位成员**：动画形象（读文件是审视姿势、写代码是奔跑、交付跳跃、停滞打瞌睡、出错倒地）、当前工具调用、耗时、模型·强度、估算成本、进度条。
- **交互飞行**：派工（队长→成员）、回报（成员→队长）、留言（写同事收件箱）、交接（读同事写的文件）、转达（队长把 A 的产出点名给 B）。
- **底部**：任务卡片、通信流（含正文）、动作流、成员名单（含自定义 agent）；点击成员看完整时间线与交付回报。
- **部门汇总 / 成本排行**：每个部门的人数、进行/完成、工具次数、tokens、缓存命中、累计用时、模型、成本；成员按估算成本排行（颜色区分模型档位）。
- **Token 与成本**：表头显示全队 token 合计与缓存命中率；每位成员卡片显示总 token 与缓存命中，详情里有输入 / 输出 / 缓存读 / 缓存写 / 命中率 / 估算成本。
- **回放**：本次会话的事件可回放；深色/浅色/自动主题。

## 自己添加成员

这套内置成员只覆盖通用软件开发；你的领域（嵌入式、数据、游戏、法务……）用看板加：先「⚙ 部门管理」建部门（可设出场规则），再「＋ 新建成员」。

看板底部「常驻成员」面板点 **＋ 新建成员**：填标识（`subagent_type`）、显示名、部门、**模型**（sonnet / opus / haiku / fable / inherit）、**思考强度**（low / medium / high / xhigh / max）、形象、简介（Claude 靠它自动委派，写清“什么时候用”）、预载技能、系统提示词。保存即写入 `~/.claude/agents/<标识>.md`（Claude Code 原生格式），并把部门/形象登记到 `team-board/team.json`。表单顶部实时预览形象、颜色、部门与模型档位。点任何成员卡片可编辑，悬停卡片右侧 ✕ 可删除（内置成员也可以删，只是文件）。**新开的 Claude Code 会话**才会加载新成员。

也可以直接手写 `~/.claude/agents/*.md`，看板会自动读到。

看板只读 `~/.claude/projects/**/subagents/agent-*.jsonl` 转录文件，不需要成员做任何"登记"，不调用任何 API。

## 目录

```
team-board/        看板：team_board.py（标准库 HTTP + 转录解析）、index.html（单文件前端）、ensure.py（钩子入口）、team.json（部门/价格配置）
agents/            8 位通用常驻成员定义（Claude Code 原生 subagent 格式）
skills/agent-team/ 团队协议：SKILL.md 步骤、roles.md 部门与模型档位、office.md 办公目录规范、lean.md 省 token 规则
CLAUDE.global.md   写入 ~/.claude/CLAUDE.md 的全局约定
install.py         安装 / 卸载
```

## 形象素材

`team-board/sprites/` 里的 4 只机器人来自 [cc-haha](https://github.com/NanmiCoder/cc-haha)（MIT）。看板会自动使用该目录下所有 8 列 × 11 行、192×208/帧 的雪碧图（行序：idle / 跑右 / 跑左 / 挥手 / 跳 / 失败 / 等待 / 工作 / 审阅）；放进更多同规格的 `.webp` 就会有更多不重复的形象。仓库不包含从其他客户端提取的素材。

## 已知限制

- Claude Code 桌面版的子代理没有互发私信的工具，所以成员交流走 `.team/inbox/` 文件；看板把写收件箱画成留言。
- 成本是按 `team.json` 里的公开单价估算，不是账单。
- 转录文件格式随 Claude Code 版本变化时需要更新 `team_board.py` 的解析。

## 致谢

思路借鉴 [cc-haha](https://github.com/NanmiCoder/cc-haha) 的 Agent Teams 工作台；token 规则借鉴 [ponytail](https://github.com/DietrichGebert/ponytail) 与 [mattpocock/skills](https://github.com/mattpocock/skills)。

MIT License
