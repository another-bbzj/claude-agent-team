# 官方 Team 模式 vs subagent 模式

只写 2026-11-11 从官方页面核对过的行为，以及本仓库看板已经读取的字段。官方页面：

- Agent Teams：<https://code.claude.com/docs/en/agent-teams>
- 并行方式（含 worktree）：<https://code.claude.com/docs/en/agents>
- 本仓库落盘说明：[docs/AGENT_TEAMS.md](../../docs/AGENT_TEAMS.md)
- 本仓库 worktree 作战区：[docs/WORKTREES.md](../../docs/WORKTREES.md)

Agent Teams 是实验功能，默认关闭。

## 判定

按顺序，命中一条就停。完成标准：汇报里写明模式名，以及停在哪一条。

1. 当前会话是非交互的 `-p`（含 Agent SDK）。官方不拉起 teammate，名为 subagent 的调用仍是普通 subagent，即使变量为 `1`。用 **subagent 模式**，并写明原因。
2. 你已经是队友，不是主会话。官方不允许嵌套团队，只有 lead 管理团队。用 **subagent 模式** 做分配给你的工单，不要再派队友。
3. 看有效的 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`。`settings.json` 的 `env` 和 shell 都能设置。用户级 `settings.json` 里的 `0` 会盖过 shell 的 export；项目设置、本地设置、`--settings` 和托管设置按官方的优先级还可以再写成 `1`。看不清有效值时用 **subagent 模式**，并写「未确认」。
4. 有效值为 `1`，且当前是交互式主会话。用 **官方 Team 模式**。
5. 其余情况用 **subagent 模式**。

主会话自己就是 lead。不要再派 `subagent_type: team-lead`。官方配置里 lead 的 agent type 就是 `team-lead`；看板（`team-board/adapters/claude_teams.py` 的 `LEAD_TYPE`）只把这个值认成 lead，多名时会警告。

## 对照

| | 官方 Team 模式 | subagent 模式 |
|---|---|---|
| 何时 | 判定第 4 条 | 判定第 1、2、3、5 条 |
| 谁在协调 | 主会话（lead 固定，不能中途更换） | 主会话，用 Agent 工具派 subagent |
| 工人 | 带 `name` 的 teammate，类型用 [roles.md](roles.md) 的 `subagent_type` | 同名 `subagent_type`，`run_in_background: true` |
| 任务 | 共享任务列表：TaskCreate / TaskUpdate / TaskList / TaskGet。状态是 pending、in progress、completed | `.team/tickets/`。不调用 TaskCreate |
| 通信 | 队友之间直接 SendMessage；邮箱在 `~/.claude/teams/<团队名>/inboxes/` | SendMessage，否则 `msg.py`，再否则 inbox 文件 |
| 上下文 | 队友不继承 lead 的对话，spawn prompt 要写清指针 | prompt 只放 office.md 的指针 |
| 隔离 | 不把队友放进各自的 git worktree。`files` 互不重叠 | 需要单独检出时，subagent 或你自己另开的会话可以用 worktree。见下面 |
| 一个会话 | 只有一个团队，不能另起名字，也不能跨会话共享 | 不建官方团队目录 |

官方还写了这些限制，模板照做，不另编一套：

- 不要手改或预先写 `~/.claude/teams/<团队名>/config.json`，下次状态更新会覆盖。可复用角色用 subagent 定义。
- in-process 队友不能随 `/resume`、`/rewind` 恢复。
- 任务状态可能滞后。看起来卡住时，先核对工作是否做完，再更新状态或让 lead 提醒对应的人。不另建一条重复任务。
- 关闭要等当前请求或工具调用结束。
- in-process 队友自己的 subagent 在前台跑。队友定义里 `background: true`，或队友请求 `run_in_background: true`，会失败或静默改成前台。这条限制的是队友，不是主会话在 subagent 模式里的后台派工。
- 分屏需要 tmux 或 iTerm2。默认是进程内，任何终端都能用。

## 验收项怎么写进官方任务

工单格式仍是 [office.md](office.md)。官方 Team 模式下一张工单对应一条共享任务。TaskCreate 的入参用官方写明的 `subject`、`description`，以及可选的 `metadata`。`owner`、`addBlockedBy`、`status` 在 TaskUpdate 上。

- `subject`：工单标题。
- `description`：SPEC 路径、工单路径，然后单独一个标题行 `验收`（或 `Acceptance`）。标题下面把工单 `accept:` 的 `- [ ]` / `- [x]` 原样抄上。普通圆点和没有复选框的句子，看板不当成验收。
- `metadata.acceptance`：与上面同一段 Markdown，**一个字符串**。不要改成去掉方括号的句子数组。看板先看任务对象上的 `acceptance` / `acceptanceCriteria` / `acceptance_criteria`，再看 `metadata` 里的同名键，最后才看 `description`。专用字段里一旦抽出条目，就不再读说明。纯句子的 `checked` 是 null；至少有一条复选框，才有 `acceptance.checklist`（`done` / `total`）。条目本身是 `acceptance.items[]` 的 `text` 和 `checked`（`true` / `false` / `null`）。
- `TaskUpdate.owner`：与拉起队友时的 `name` 逐字相同。看板的 `ownerMatch` 只认成员 `name` 或官方 lead 名字的完全一致，多一个空格就不算。对上之后才可能写 `estimate`。
- `TaskUpdate.addBlockedBy`：工单 `blocked_by` 里已创建任务的 id。
- `TaskUpdate.status`：开工写 `in_progress`，做完写 `completed`。用 TaskCreate 返回的任务 id（入参键是 `taskId`）。看板从转录里的 TaskCreate / TaskUpdate 留下 id、状态、owner 和时间，用这段「进行中」给 `diff` 和 `estimate` 划窗口。

看板自己补上、模板不要写进任务文件的字段：

| 快照字段 | 何时出现 | 键 |
|---|---|---|
| `acceptance` | 按上面的顺序真抽出了条目 | `items`（`text`、`checked`）；有复选框时还有 `checklist`（`done`、`total`） |
| `estimate` | `owner` 与成员 name 或 lead 名字完全一致，并且能对上该名字在 `in_progress` 期间的转录用量 | `cost`、`tokens`、`status`、`partial`、`unpriced`、`basis`。对不上区间时 `status` 为 `unknown`，`cost` 为 null。这是估算，不是账单 |
| `diff` | 本地 git 能读到已跟踪改动 | `files`、`insertions`、`deletions`、`status`、`partial`、`basis`。官方任务按进行中时间窗；没有窗口时才可能退到 `branch` 或 `worktree`，那是整份差异、标成部分。git 不可用时 `status` 为 `unavailable`，数字是 null，不写成 0 |

没有转录的官方队友卡没有 token，成本保持未知。`owner` 写对了也不会凭空出现金额。

工单文件若要让看板的工单行统计改动，保留 `files:`，并增加 `## Owned files`，一行一个仓库内相对路径。这一节不写 `..` 或绝对路径。官方任务的 `diff` 不读这节，它看的是状态时间窗。

## 隔离

官方 Agent Teams 不把队友隔离到各自的 git worktree。同一团队里两人改同一文件会互相覆盖，所以工单 `files` 必须互不重叠。

需要各自的检出时，不要在这个团队里再套一层。按 [docs/WORKTREES.md](../../docs/WORKTREES.md) 用 `claude --worktree <name>` 或 `git worktree add` 另开会话。看板把同一仓库的多路会话收成作战区，分组的是会话，不是同一官方团队里的队友。
