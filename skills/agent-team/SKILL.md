---
name: agent-team
description: 用 Agent Team（部门制多代理）完成一个开发任务。当用户说"用 agent team / 团队 / 多代理 / 分头做 / 并行开发"，或任务明显需要 3 个以上独立模块并行时使用。定义部门完整性门禁、模型分级、.team 办公协议（工单/收件箱/交接单）与 token 预算。
---

# Agent Team 协议

参考：[roles.md](roles.md)（部门、模型档位、何时必须出场）· [office.md](office.md)（`.team/` 办公目录：工单、收件箱、交接单、汇报模板）· [lean.md](lean.md)（token 规则）。看板 http://127.0.0.1:7788/ ，派工前用浏览器工具打开；没监听就 `python ~/.claude/team-board/ensure.py`。

## 步骤

1. **定规模**。S（≤2 个文件，一人能做）→ 不开 team，直接做并说明原因。M（3-6 个模块）→ 队长自己写 SPEC。L（>6 模块或陌生领域）→ 先派 `team-architect` 出 SPEC 与工单。完成标准：给出 S/M/L 与成员上限（M ≤ 6 人，L ≤ 10 人）。
2. **建办公室**。按 [office.md](office.md) 在项目里创建 `.team/`：`SPEC.md`（接口签名、数据模型、DOM/class 约定、测试命令）、`tickets/NN-slug.md`（每张工单是一条**竖切片**，声明 `blocked_by`，大小能装进一个新上下文窗口）、每位成员一个空 `inbox/<subagent_type>.md`。完成标准：每个待改文件恰好属于一张工单。
3. **第一波派工**（无阻塞的工单）。每次 Agent 调用：`subagent_type` 用 [roles.md](roles.md) 里的名字；`description` 为中文工单标题（看板任务名）；prompt 只放**指针**——SPEC 路径、工单路径、收件箱路径——加 [office.md](office.md) 的派工模板；`run_in_background: true`。成员之间通过收件箱和交接单交流（桌面版子代理没有私信工具，实测发不出去）。
4. **收报与转达**。成员汇报 ≤ 200 字。汇报里"需要转达给 X"的条目，队长追加进 `inbox/X.md`（或下一次派工的 prompt 里点名引用），看板会画转达线。阻塞解除的工单立即派出。
5. **门禁**（不满足不得宣布完成）：≥2 人写了代码 → 必须派 `code-reviewer` + `qa-tester`；交付给人用 → 必须派 `docs-writer`；成员失败/停滞 → 重派同一角色并把上下文写全。完成标准：`.team/tickets/` 每张状态为 done，门禁部门都有交接单。
6. **队长验收**。亲自跑测试、亲自走主流程；`curl -s http://127.0.0.1:7788/snapshot.json` 读 `totals`（成员成本、模型构成），与第 1 步的成员上限对照。向用户汇报：每人一两句结论、并行暴露的接缝问题、未修隐患、本次成本。
