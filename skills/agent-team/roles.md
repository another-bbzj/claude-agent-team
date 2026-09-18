# 部门与常驻成员

成员定义在 `~/.claude/agents/<subagent_type>.md`（全项目通用，各自带 `model`/`effort`/预载 skills，`memory: user` 跨项目积累经验）。用户在 `~/.claude/agents/` 或项目 `.claude/agents/` 新增的 agent，description 与任务匹配时优先使用。

| 部门 | 成员 | subagent_type | 模型 · 强度 | 何时出场 |
|---|---|---|---|---|
| 指挥部 | 队长（主会话）、阿图·架构师 | `team-architect` | opus · high | L 级任务：出 SPEC 与工单 |
| 研发部 | 阿服·后端 / 小界·前端 | `backend-dev` / `frontend-dev` | sonnet · medium | 任何写代码的任务（任意语言：数据层、算法、脚本、界面） |
| 质量部 | 测测·测试 / 老审·审查联调 | `qa-tester` / `code-reviewer` | sonnet · medium / opus · high | ≥2 人写了代码：收尾建议派测测 + 老审 |
| 研究部 | 探探·研究分析 | `researcher` | haiku · low | 调研、读外部代码、查数据手册 |
| 文档部 | 文文·文档 | `docs-writer` | haiku · low | 交付给他人使用 |
| 运维部 | 发发·发布 | `release-ops` | sonnet · low | 构建、打包、CI、烧录脚本 |

## 模型分级

- haiku · low：调研、文档、整理、格式转换。
- sonnet · medium：常规实现、测试、脚本、驱动。
- sonnet · high：需要仔细推理但不需要最强模型：算法、状态机、复杂样式。
- opus · high：架构拆解、联调审查、疑难 bug。
- 临时改档：Agent 工具的 `model` 参数覆盖定义（简单页面可用 haiku 跑 `frontend-dev`）。
- 成员上限：M 级 ≤ 6 人，L 级 ≤ 10 人。超出说明拆分过细，合并工单。

## 出场是建议不是门禁

每个任务用到的部门不一样，用不到的不必凑人；看板上空着的部门只显示「按需出场」，收尾后最多提示「建议补位」。跳过时在汇报里说一句原因。没有合适的常驻成员就派 `general-purpose`，`description` 写「部门名·任务标题」（或「[部门id] 任务」），看板会归进那个部门；部门不存在先 `POST http://127.0.0.1:7788/api/departments`（JSON：id / name / icon）建一个，立即生效。

## 角色通用性

角色按**职能**定义而非按项目：`backend-dev` 写任何语言的逻辑模块，`code-reviewer` 审任何代码。换项目只换 SPEC 和工单，不换人。

你的领域缺角色时（嵌入式、数据科学、游戏、法务……），在看板「部门管理」里加一个部门，再用「＋ 新建成员」创建成员：填简介（何时用它）、模型、思考强度、预载你自己的 skill；或者直接手写 `~/.claude/agents/<name>.md`。
