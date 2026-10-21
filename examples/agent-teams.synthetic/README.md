# 合成的 Agent Teams 目录

这棵目录是假数据，用来对照 [`docs/AGENT_TEAMS.md`](../../docs/AGENT_TEAMS.md)。不要把它复制进真实的 `~/.claude`。

`claude-home/` 对应官方文档里的 `~/.claude`，团队名 `session-a1b2c3d4` 取自 hooks 文档的示例，不是某个真实会话。

官方文档没有公布 `config.json` 成员对象、任务目录里的文件名，以及邮箱条目的 JSON 键。这里的键名分成两类：

- `members`、任务工具的 `id` / `subject` / `status` / `blockedBy` / `blocks` / `description` / `owner`：官方文档里出现过的名字。任务文件用的是工具输出的形状，不表示磁盘上一定叫 `tasks.json` 或 `task-get.json`。
- `name`、`agentId`、`agentType`、`agent_id`、`agent_type`：文档描述了这些概念，但没有给出 `config.json` 的键。fixture 混用两套拼写，是为了让读取方必须都接受。`unspecified` 是故意留下的未知键，读取方应原样保留。

邮箱条目没有使用任何自称「真实字段」的键。
