---
name: agent-team
description: 用 Agent Team（部门制多代理）完成一个开发任务。当用户说"用 agent team / 团队 / 多代理 / 分头做 / 并行开发"，或任务明显需要 3 个以上独立模块并行时使用。定义部门出场建议、临时角色与部门的创建、模型分级、.team 办公协议（工单/收件箱/交接单）与 token 预算。
---

# Agent Team 协议

参考：[roles.md](roles.md)（部门、模型档位、何时出场、临时角色）· [office.md](office.md)（`.team/` 办公目录：工单、收件箱、交接单、汇报模板）· [lean.md](lean.md)（token 规则）。看板是可选的本机可视化工具：确认已安装 `~/.claude/team-board/ensure.py` 后，派工前运行 `python ~/.claude/team-board/ensure.py --open`。启动成功才提供 `👉 **[Agent Team 指挥室](http://127.0.0.1:7788/)**` 链接；脚本缺失或启动失败时说明一次，继续用 `.team/` 工单、收件箱和交接单，不为此安装软件或修改全局配置。当前看板解析 Claude Code 转录；其他环境使用文件协作不代表已接入看板。

先检查宿主是否提供原生子代理工具和所需角色。支持时按用户允许的并行方式派工；工具或角色不可用时，由当前代理依次完成实现、测试、审查等角色步骤，并明确报告为串行角色流程，不虚构子代理。模型选择与成员上限服从用户约束，不因看板预算建议自动覆盖用户已指定的模型。

## 步骤

1. **定规模**。S（≤2 个文件，一人能做）→ 不开 team，直接做并说明原因。M（3-6 个模块）→ 队长自己写 SPEC。L（>6 模块或陌生领域）→ 先派 `team-architect` 出 SPEC 与工单。完成标准：给出 S/M/L 与成员上限（M ≤ 6 人，L ≤ 10 人）。
2. **建办公室**。按 [office.md](office.md) 在项目里创建 `.team/`：`SPEC.md`（接口签名、数据模型、DOM/class 约定、测试命令）、`tickets/NN-slug.md`（每张工单是一条**竖切片**，声明 `blocked_by`，大小能装进一个新上下文窗口）、每位成员一个空 `inbox/<subagent_type>.md`。仓库中有 `tools/team_workspace.py` 时，可选用 `python tools/team_workspace.py --plan PLAN.json --output 项目目录` 从计划生成工单；它拒绝已有 `.team`，不会合并或覆盖。没有该工具时按模板手工创建。完成标准：每个待改文件恰好属于一张工单。
3. **第一波派工**（无阻塞的工单）。每次 Agent 调用：`subagent_type` 用 [roles.md](roles.md) 里的名字；`description` 为中文工单标题（看板任务名）；prompt 只放**指针**——SPEC 路径、工单路径、收件箱路径——加 [office.md](office.md) 的派工模板；`run_in_background: true`。成员之间可用原生 `SendMessage` 或 `msg.py` CLI 交流，看板通讯频道汇聚所有消息（详见 office.md）。
4. **收报与转达**。成员汇报 ≤ 200 字。汇报里"需要转达给 X"的条目，队长追加进 `inbox/X.md`（或下一次派工的 prompt 里点名引用），看板会画转达线。阻塞解除的工单立即派出。
5. **收尾检查**（建议而非硬性）：≥2 人写了代码 → 收尾时派 `code-reviewer` + `qa-tester`；交付给人用 → 派 `docs-writer`；成员失败/停滞 → 重派同一角色并把上下文写全。用不到的部门**不用凑人**——每个任务出场的部门本来就不同，看板上空着的部门只显示“按需出场”，收尾后最多提示“建议补位”，不是错误。跳过时在汇报里说一句原因即可。完成标准：`.team/tickets/` 每张状态为 done。
6. **临时角色与部门**。常驻成员里没有合适的人时，直接派 `general-purpose`，`description` 写成「部门名·任务标题」（如「设计部·画登录页原型」「[rnd]·对比三种排序算法」），看板可用时会把它归进那个部门。只有看板已运行且本次需要持久登记时才调用下列接口；无看板时用工单中的角色标签继续，不请求端口或创建全局角色。需要登记而部门不存在时先 `curl -s -X POST http://127.0.0.1:7788/api/departments -H 'Content-Type: application/json' -d '{"id":"design","name":"设计部","icon":"🎨"}'` 建一个（立即生效）。一个角色会反复用到就顺手 `POST /api/agents` 登记成常驻成员（下次会话起可直接点名）。1-3 人的小团队同样正常：看板只画出场的部门，统计表按实际人数算。
7. **队长验收**。亲自跑测试、亲自走主流程；看板可用时才读 `http://127.0.0.1:7788/snapshot.json` 的 `totals`，与第 1 步的成员上限对照。向用户汇报：实际参与者或串行角色步骤的结论、接口接缝、未修隐患和已测成本；拿不到成本时写「未测量」。`budget.status` 为 `warn` 或 `over` 时结合 `budget.suggestions` 缩小后续工单，模型调整遵守用户选择，不把估算值当作账单或自动停止执行的硬限额。
