# 全局约定

## Agent Team（部门制多代理）

- 用户说"用 agent team / 团队 / 多代理 / 分头做"，或任务需要 ≥ 3 个独立模块并行时：**先调用 `agent-team` 技能**（`Skill agent-team`），按它的部门表、出场建议、模型分级执行。S 级小任务不要开 team，直接做并说明原因。
- 常驻部门成员在 `~/.claude/agents/`：指挥部 team-architect；研发部 backend-dev / frontend-dev；质量部 qa-tester / code-reviewer；研究部 researcher；文档部 docs-writer；运维部 release-ops；以及用户在看板里自建的成员。各自带 model 与 effort；派工时 `subagent_type` 直接点名，`description` 写中文（看板任务标题）。用户自建的 agent 与任务匹配时优先使用。
- 成员之间靠项目里的 `.team/` 办公目录交流（工单 tickets/、收件箱 inbox/<subagent_type>.md、交接单 handoffs/），细则在技能的 office.md；派工 prompt 只给路径指针，成员汇报 ≤ 200 字。
- 看板 http://127.0.0.1:7788/ 由 SessionStart 钩子常驻（代码 `~/.claude/team-board/`）。派 ≥ 2 个子代理前先运行 `python ~/.claude/team-board/ensure.py --open`（保证服务在跑并在用户浏览器打开看板；桌面版还可再用浏览器工具打开同一地址），并在回复里给出可点击的链接 `👉 **[Agent Team 指挥室](http://127.0.0.1:7788/)**`（链接文字，不要只贴裸网址）。
- 成员回报后，在对话里用一两句话转述每个人的结论，不要只说"完成了"。≥2 人写了代码时收尾建议派质量部复核、交付给人用时建议派文档部；用不到的部门不必凑人，跳过说明原因即可。常驻成员里没有合适的人就派 `general-purpose`，`description` 写「部门名·任务标题」，看板会归进对应部门；需要新部门用看板的 `POST /api/departments` 建。
