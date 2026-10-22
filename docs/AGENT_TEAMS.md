# 官方 Claude Code Agent Teams 的落盘格式

本文只记录 Anthropic 官方文档写明的磁盘布局，以及文档**没有**写明、因此不能当成 schema 的部分。合成样例在 [`examples/agent-teams.synthetic/`](../examples/agent-teams.synthetic/README.md)。样例里没有任何真实会话、密钥或本机路径。

## 调查范围

- 抓取日期：2026-10-21。
- 同日 [Claude Code changelog](https://code.claude.com/docs/en/changelog) 页面顶部版本是 **2.1.304**。这是文档站点当时列出的最新版本，不是本仓库在某台机器上执行 `claude --version` 的结果。
- 下文的版本号都来自这些文档页面里的「Requires / Before / As of」句子，不是对本仓库的兼容性测试。

对照过的页面：

| 页面 | URL |
| --- | --- |
| Agent Teams | <https://code.claude.com/docs/en/agent-teams> |
| `.claude` 目录 | <https://code.claude.com/docs/en/claude-directory> |
| Hooks（`TaskCreated` / `TaskCompleted` / `TeammateIdle`） | <https://code.claude.com/docs/en/hooks> |
| Agent SDK TypeScript 工具类型 | <https://code.claude.com/docs/en/agent-sdk/typescript> |
| Todo / Task 工具流 | <https://code.claude.com/docs/en/agent-sdk/todo-tracking> |
| 错误说明（写不进 teammate inbox） | <https://code.claude.com/docs/en/errors> |
| 并行方式对比 | <https://code.claude.com/docs/en/agents> |

非官方逆向文章里出现过逐任务文件名、锁文件和邮箱字段。那些键**没有**写进官方文档，本文不采用，解析时也不要求它们存在。

## 开关

Agent Teams 是实验功能，默认关闭。设置环境变量 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`（shell 或 `settings.json` 的 `env`）之后才会在会话开始时建立团队目录。未设置时，官方说明是：不会建立团队、不会写团队目录，也不会拉起 teammate。

`~/.claude` 在 Windows 上对应 `%USERPROFILE%\.claude`。若设置了 `CLAUDE_CONFIG_DIR`，文档中的 `~/.claude` 路径改到该目录下。本仓库看板的转录根目录仍是 `Path.home() / '.claude'`，与现有会话发现保持一致；显式传入根目录的读取函数不受这个限制。

## 目录

官方用会话 ID 派生团队名：`session-` 加上会话 ID 的前 8 个字符。Hooks 文档里的示例值是 `session-a1b2c3d4`。一个会话只有一个团队，不能另起名字，也不能跨会话共享（Limitations：One team per session）。

| 路径 | 官方说明 |
| --- | --- |
| `~/.claude/teams/{team-name}/config.json` | 团队配置。会话结束时删除整个团队配置目录。 |
| `~/.claude/teams/{team-name}/inboxes/{agent-name}.json` | 每个 agent 一个邮箱 JSON 文件。 |
| `~/.claude/tasks/{team-name}/` | 任务列表目录。会话结束后仍留在本地，且不会上传；保留时间跟转录一样，由 `cleanupPeriodDays` 决定。 |

`~/.claude/tasks/` 在目录说明里是「task tools 写下的任务列表，一个列表一个目录」。`cleanupPeriodDays` 默认 30 天，最小 1；设为 `0` 会校验失败。

会话结束时，Shutdown 一节说共享目录会自动清理，并指向 Architecture 区分哪些目录删除、哪些留下。Architecture 的区分是：配置目录删除，任务目录留下。

项目目录里的 `.claude/teams/teams.json` **不是**团队配置。官方写明 Claude 把它当成普通文件。

这些目录由 Claude Code 在会话启动时生成，并在 teammate 加入、空闲或离开时更新。不要手改 `config.json`，也不要预先写好它：下次状态更新会覆盖。可复用的角色应写 subagent 定义，而不是手写团队配置。

Agent Teams **不会**把 teammate 隔离到各自的 git worktree。需要分文件所有权，避免两人改同一文件（<https://code.claude.com/docs/en/agents>）。

## `config.json` 里已经写明的结构

- 顶层有一个 `members` 数组。这是文档给出的 JSON 键。
- 每个成员有名字（name）和 agent ID。文档没有给出这两个值的 JSON 键名。
- lead 的条目始终带有 agent type，值为 `team-lead`。文档没有给出 agent type 的 JSON 键名。
- teammate 的 agent type 是 lead 创建它时起的名字（内置类型或 subagent 定义）。lead 没有起名字时，这个字段省略。
- 文件里还有运行时状态，「例如 session ID 和 tmux pane ID」。文档没有给出这些值的 JSON 键名。
- teammate 可以读这个文件来发现其他成员。

因此下面这些键是**候选**，不是已公布的文件 schema。合成 fixture 故意混用它们，用来固定「读取方必须都接受，且不能因为缺了某一个就失败」：

| 概念 | 候选键 | 候选键的出处（不是 `config.json` schema） |
| --- | --- | --- |
| 名字 | `name`，其次 `teammate_name` | 文档用词 “name”；hook 字段 `teammate_name` |
| agent ID | `agentId`，其次 `agent_id` | Agent 工具输出 `agentId`；hook 公共字段 `agent_id` |
| agent type | `agentType`，其次 `agent_type` | Agent 工具输出 `agentType`；hook 字段 `agent_type`。lead 的值必须是 `team-lead` 才视为 lead |

大小写不额外归一。`Team-Lead` 不是文档中的 lead 标记。未知键原样保留。

## 任务

### 目录里有什么：未知

官方只公布了目录 `~/.claude/tasks/{team-name}/`，没有公布目录里面的文件名、是一个文件还是多个文件、以及有没有锁文件或计数文件。合成 fixture 使用 `tasks.json` 和 `task-get.json` 只是为了放进两种**工具输出**形状，不表示 Claude Code 会写这两个文件名。

### 工具输入输出里公布的字段

这些类型来自 Agent SDK 的工具 schema（Claude Code v2.1.268 起的默认 Task 工具集合；TypeScript SDK 从 v0.3.268 起打包该行为）。它们描述的是工具调用，不是任务目录的文件格式。读取目录时，只有 JSON 里真的出现这些键才采用。

`TaskCreate` 输入：`subject`、`description`，可选 `activeForm`、`metadata`。

`TaskUpdate` 输入：`taskId`，可选 `status`（`pending`、`in_progress`、`completed`、`deleted`）、`subject`、`description`、`activeForm`、`addBlocks`、`addBlockedBy`、`owner`、`metadata`。`status: "deleted"` 表示删除。

`TaskGet` 输出的 `task`：`id`、`subject`、`description`、`status`（`pending`、`in_progress`、`completed`）、`blocks`、`blockedBy`。找不到 ID 时 `task` 为 `null`。

`TaskList` 输出的 `tasks[]`：`id`、`subject`、`status`（同上三种）、可选 `owner`、`blockedBy`。

`TaskCreate` 输出只保证 `task.id` 和 `task.subject`。

Todo 文档还写了：模型流里可能出现 `id` / `task_id`（应视为 `taskId`）和 `active_form`（应视为 `activeForm`）。这是工具入参的容错，不是文件格式。

Agent Teams 正文把任务状态说成 pending、in progress、completed 三种，并写明有依赖的 pending 任务在依赖完成前不能被认领；认领时用文件锁避免竞争。正文没有给出锁文件的名字。`in progress`（带空格）和类型里的 `in_progress` 同时出现在官方文档中，读取时把带空格的写法折叠成 `in_progress`，原始字符串仍保留。

`owner` 在 `TaskList` / `TaskUpdate` 里是可选字符串。文档没有写它一定等于成员的 name。只有在它与某个成员的 name 完全一致时，才把这条任务算到该成员身上。

Hook 载荷（同样不是任务文件）在 `TaskCreated` / `TaskCompleted` 上提供 `task_id`、`task_subject`，可选 `task_description`、`teammate_name`、`team_name`。`team_name` 已标记为废弃。`agent_id` 在这两个事件上标识创建或完成任务的 subagent / in-process teammate，可能缺失，且需要 Claude Code v2.1.290 或更新。

## 邮箱

路径已公布：`~/.claude/teams/{team-name}/inboxes/{agent-name}.json`。文件名中的 `{agent-name}` 是文档里的路径成分。

官方行为：

- 读邮箱时逐条校验。不符合 message format 的条目记为错误并从文件中删除，其余消息仍投递。
- v2.1.207 之前，一条损坏的邮箱记录会每秒报错并挡住该邮箱，直到手动删文件。
- 只有写入收件人邮箱文件成功，才算发送成功。磁盘满、目录不可写，或 v2.1.224 之前「写失败仍报已发送」，都在错误文档的 “Failed to write to a teammate's inbox” 一节。
- 消息可以是纯文本，也可以是结构化协议消息。错误文档点名的协议消息有：plan approval、plan rejection、shutdown request、shutdown rejection、plan approval request、permission request，以及 shutdown confirmation。这些是错误文案里的名称，不是 JSON 键。

**条目本身的 JSON 键没有公布。** 不要假定存在 `from`、`text`、`timestamp` 或 `read`。读取方应把数组元素或整个 JSON 值原样保留；坏掉的文件记一条警告，不要抛出导致看板崩溃。

`SendMessage` 工具有 `to`，以及可选的 `summary`（约 5–10 个词，超过 200 字符会截断）。那是工具入参，不是邮箱文件 schema。

## 其他已经写明、但不在这三个文件里的行为

这些句子影响产品行为，没有对应到上面的 JSON 键，本文不把它们编成字段：

- 分屏从 v2.1.186 起把 lead 的 effort 传给 split-pane teammate；更早的版本不传。
- v2.1.199 起，看着某个 teammate 时输入 `/model` 或 `/fast` 会提示改的是 lead。
- in-process teammate 从 v2.1.296 起才会应用 subagent 定义里的 `autoCompactWindow`；split-pane teammate 忽略该字段。
- 限制：in-process teammate 不能随 `/resume`、`/rewind` 恢复；任务状态可能滞后；关闭可能要等当前请求结束；不能嵌套团队；lead 不能中途更换；teammate 启动时继承 lead 的权限模式，但 `dontAsk` 模式不继承（[Agent Teams](https://code.claude.com/docs/en/agent-teams) 的 Permissions 一节；该句没有单独的版本号）；分屏需要 tmux 或 iTerm2。

## 合成 fixture 约定

`examples/agent-teams.synthetic/claude-home/` 模仿 `~/.claude` 的相对布局。团队名 `session-a1b2c3d4` 取自 hooks 文档的示例。`session-c0ffee01` 是本仓库另起的合成团队，用来离线复现只读看板的依赖链、跨文件重复任务 id、TaskList 与 TaskGet 两种形状，以及一名没有任务 owner 对上的 teammate。文件清单在 [`examples/agent-teams.synthetic/README.md`](../examples/agent-teams.synthetic/README.md)。超过 2MB 的任务文件不进 git，测试在临时目录里生成。

- `config.json` 的 `members` 里，`session-a1b2c3d4` 有一名 `team-lead`、一名写了 agent type 的 teammate，以及一名省略 agent type 的 teammate。两名有类型的成员分别使用上表里的两套候选键。`session-c0ffee01` 在 lead 和两名有任务的 teammate 之外还有 `synthetic-observer`，没有任何任务的 `owner` 等于这个名字。
- 两个团队的任务目录都同时放了 `TaskList` 输出形状（`tasks` 数组）和 `TaskGet` 输出形状（`task` 对象）。文件名是本仓库起的。
- `session-a1b2c3d4` 的邮箱是 JSON 数组。元素是不透明的字符串或对象，用来保证读取方不依赖未公布的键。

测试 `tests/test_agent_teams_fixture.py` 检查这棵树能被解析为 JSON，包含上述角色、状态和 `session-c0ffee01` 的看板情形，并且拒绝绝对家目录、邮箱样式、`sk-` 以及其它密钥样式。

## 看板怎么用这些文件

`team_board.py` 仍以「有 subagent 转录的会话」作为入口。选中会话后，用会话 ID 的前 8 个字符寻找 `teams/` 和 `tasks/` 里的同名团队，把 teammate 加成成员卡，并带上 `officialTeam: true`。界面在卡片和任务条上显示「官方 Team」。lead 已经是主会话那张卡，不会再占一张 teammate 卡。

这些卡没有转录，所以没有 token，成本保持未知。`owner` 只有和成员 name 完全一致时才用来推断进行中或已完成；对不上就显示待命。

选中的会话能对上 `tasks/{team-name}/` 时，部门表下面多一行**只读看板**。三列是 pending、in progress、completed（界面写作待处理、进行中、已完成）。`deleted` 和认不出的状态不进这三列。每张卡片有负责人（没有 `owner` 就是未认领；`owner` 和成员 name 或 lead 名字完全一致时算对上了人）、依赖或阻塞，以及在当前状态停留了多久。阻塞只指 pending 且 `blockedBy` 里还有没完成的 id，对应文档里「依赖完成前不能认领」。卡片不能拖。`/api/tasks` 的任意方法都返回 405，并带 `Allow: GET`；正文不是 JSON 时也是 405，不会改任务目录。

单个任务文件超过 2MB 会跳过并警告，不把内容读进内存。三列合计超过 500 条时只显示前面的 500 条，并警告。同一个任务 id 出现多次时，只保留文件修改时间较新的那一条；时间相同或都没有，就留后读到的那条。警告里只写 id，不写路径。是否算完成，按留下来的那条计算，所以较旧的 completed 不会把较新的 pending 依赖当成已经完成。一个文件里有几条，按去重之前的条数算：两条里丢掉一条，剩下的仍按「大约」计时。

官方文件没有「进入当前状态」的时间。看板进程第一次读到某条任务的当前状态时，用它所在文件的修改时间当作起点：文件里只有这一条，就按这个时间计；同一个文件里有多条，界面会标成大约，因为分不出是哪一条被改写。之后只有状态变了，才从看板看到变化的那一刻重新计时。这个记忆只在看板进程里，重启后回到文件修改时间，也不会写回 `~/.claude/tasks/`。换到另一个会话时，上一支团队的计时会清掉；当前会话没有任务目录时同样清掉。

快照里的 `officialTeam` 带 `readOnly`、团队名、lead 名字、警告，以及任务的 id / subject / description / status / normalizedStatus / owner / blockedBy / blocks / blocked / waitingOn / stateSince / stateSinceKind / stateSeconds。不带 `raw`、`source`、`sourceMtime` 或未公布的运行时字段。页面约每 1.5 秒重新取 `/snapshot.json`，看板跟着这一次刷新走，没有另外的写入接口。

只有官方团队、没有 subagent 转录的会话，这次不会单独出现在会话列表里。只读演示快照放了两名合成的官方 teammate，以及三张合成任务卡，不读取本机 `~/.claude`。
