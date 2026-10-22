# 合成的 Agent Teams 目录

这棵目录是假数据，用来对照 [`docs/AGENT_TEAMS.md`](../../docs/AGENT_TEAMS.md)，也给只读任务看板的测试当任务文件。不要把它复制进真实的 `~/.claude`。

`claude-home/` 对应官方文档里的 `~/.claude`。团队名都是合成的：`session-a1b2c3d4` 取自 hooks 文档的示例；`session-c0ffee01` 是本仓库另起的名字，不是某个真实会话。

```
claude-home/
  teams/session-a1b2c3d4/config.json
  teams/session-a1b2c3d4/inboxes/synthetic-lead.json
  teams/session-a1b2c3d4/inboxes/synthetic-researcher.json
  teams/session-a1b2c3d4/inboxes/synthetic-writer.json
  tasks/session-a1b2c3d4/tasks.json       TaskList 形状（tasks 数组）
  tasks/session-a1b2c3d4/task-get.json    TaskGet 形状（task 对象）
  teams/session-c0ffee01/config.json
  tasks/session-c0ffee01/task-list.json   TaskList 形状：依赖链，以及 id 12 的旧副本
  tasks/session-c0ffee01/task-get.json    TaskGet 形状：同一个 id 12 的另一份
```

官方文档没有公布 `config.json` 成员对象、任务目录里的文件名，以及邮箱条目的 JSON 键。这里的键名分成两类：

- `members`、任务工具的 `id` / `subject` / `status` / `blockedBy` / `blocks` / `description` / `owner`：官方文档里出现过的名字。任务文件用的是工具输出的形状，不表示磁盘上一定叫这些名字。
- `name`、`agentId`、`agentType`、`agent_id`、`agent_type`：文档描述了这些概念，但没有给出 `config.json` 的键。`session-a1b2c3d4` 混用两套拼写，是为了让读取方必须都接受。`unspecified` 是故意留下的未知键，读取方应原样保留。

邮箱条目没有使用任何自称「真实字段」的键。已提交文件里不应出现绝对家目录、邮箱样式或密钥样式的字符串。`tests/test_agent_teams_fixture.py` 会检查整份 `examples/`。

## session-a1b2c3d4

文档示例团队。一名 `team-lead`、一名写了 agent type 的 teammate、一名省略 agent type 的 teammate。任务 1 已完成，2 进行中并依赖 1，3 和 4 待处理并依赖 2。`tasks.json` 是 TaskList 的 `tasks` 数组，`task-get.json` 是 TaskGet 的 `task` 对象。

## session-c0ffee01

只读看板要离线复现的几件事都在这个团队里：

- **依赖链**。`task-list.json` 里 10、11 已完成，12 的这一份也标成已完成。13 依赖 12，14 依赖 13。15 只依赖已经完成的 10 和 11。14 没有 `owner`，是未认领。
- **跨文件重复的任务 id**。`task-get.json` 仍是 id 12，但是 TaskGet 形状，状态为 `in_progress`，subject 与列表里的旧副本不同，并带有 `blocks`。解析目录时两份都会留下。看板只留修改时间较新的一份；时间相同则留后读到的一份。Git 不保存可复现的修改时间，测试把这两份复制到临时目录后，把 `task-get.json` 的时间设得更晚。留下来的 12 还在进行中，所以 13、14 仍然被挡住；15 不被挡住。
- **两种工具输出形状**。列表文件是 TaskList 的 `tasks` 数组，另一份是 TaskGet 的 `task` 对象。文件名是本仓库起的。
- **没有对上 owner 的 teammate**。`synthetic-observer` 在 `members` 里，没有任何任务的 `owner` 等于这个名字，看板状态是待命，说明仍是「官方 Agent Team 成员」。任务 16 的 `owner` 是 `synthetic-not-on-team`，这个名字不在 `members` 里。
- **超过 2MB 的任务文件**。这种文件不提交。已提交的本目录文件保持在 64KB 以内。测试在临时目录生成一份超限文件，放在上述小文件旁边，确认它被跳过，小文件仍在。
