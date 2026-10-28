# 可选的 Claude Code 实时钩子

看板默认只在转录写到 `~/.claude/projects/` 之后才看得到动作。这条通道是可选的：Claude Code 在工具调用和子代理结束时立刻通知本机看板。不装它，安装和看板行为与以前一样。

字段以 Claude Code 的 [Hooks reference](https://code.claude.com/docs/en/hooks) 为准（同一份文档也曾放在 `https://docs.claude.com/en/docs/claude-code/hooks`）。下面只使用文档里写明的输入字段，没有自造键名。

## 装上和卸下

```bash
python install.py --hooks
```

这会先做普通安装，再在 `~/.claude/settings.json` 的 `hooks` 里合并这些事件：

| 事件 | 文档里的含义 | 看板用来显示什么 |
|---|---|---|
| `PreToolUse` | 工具执行前 | 成员开始调用某个工具 |
| `PostToolUse` | 工具成功之后 | 成员这次调用已经返回 |
| `SubagentStop` | 子代理结束 | 该子代理停了 |
| `Notification` | Claude Code 发出通知 | 只显示 `notification_type` |
| `Stop` | 主会话结束本轮 | 队长这一轮停下 |

每个事件追加一个分组，**不改你原来的分组和 matcher**。处理脚本是 `~/.claude/team-board/hook_relay.py`，用 exec 形式调用（`command` + `args`，不经过 shell）：

```json
{
  "type": "command",
  "command": "python",
  "args": ["<你的家目录>/.claude/team-board/hook_relay.py"],
  "timeout": 2
}
```

`timeout` 是 Claude Code 的上限（秒）。脚本自己连不上看板时会在远小于这个值的时间内退出。不设置 `async`、`onFailure` 或 `statusMessage`：避免后台残留进程，也避免在每次工具调用时闪一条状态。

重复运行 `python install.py --hooks` 不会再加一条。之后的普通 `python install.py` **不会**删掉已经装上的实时钩子，也 **不会**在你没加过 `--hooks` 时突然写上它们。

`python install.py --uninstall` 会和 SessionStart 一起卸掉这些事件里属于本工具的钩子，你自己的钩子留在原来的分组里。

## 脚本保证不改变 Claude Code 的决定

命令钩子的 stdout 会被当成决策。`PreToolUse` 和 `Stop` 尤其如此：退出码 2，或者打出 `decision` 一类的 JSON，会拦住工具或让这一轮继续。

`hook_relay.py` 因此：

- 成功、失败、看板没开、stdin 不是合法 JSON，都是退出码 0
- stdout 和 stderr 都是空的
- 用 `ProxyHandler({})` 直连 `127.0.0.1`，不走 `http_proxy` / 系统代理（和 `ensure.py` 一样）
- 请求带 `Origin: http://127.0.0.1:<端口>`，端口来自 `TEAM_BOARD_PORT`，默认 `7788`
- 套接字超时 0.2 秒。连不上就返回，不重试

## 传什么，不传什么

钩子 stdin 里常见的还有 `tool_input`、`tool_response`、`message`、`last_assistant_message`、`transcript_path`、`cwd`。这些都不转发。

留下的只有：

| 字段 | 来源 |
|---|---|
| `session_id` | 公共输入 |
| `event` | 公共输入里的 `hook_event_name` |
| `tool_name` | `PreToolUse` / `PostToolUse` |
| `tool_use_id` | 同上，见下面的「还没和转录合并」 |
| `agent_id` / `agent_type` | 公共输入；子代理和队内成员会带 |
| `notification_type` | 只在 `Notification` |
| `stop_hook_active` | 只在 `Stop` / `SubagentStop`，布尔值 |
| `ts` | 中继收到时的本机时间。钩子 JSON 本身没有这个时间字段 |

扫描只看 JSON 的前 256KB，用来躲开巨大的工具输出；标识符如果只出现在这之后，就丢掉，而不是把正文留下来。stdin 会一直读到结束，避免管道写满把 Claude Code 堵住。单条 POST 不超过 4KB。

## 看板怎么收

`POST /api/hooks/event`

- 进程只监听 `127.0.0.1`
- 对端地址也必须是回环地址
- `Origin` 必须存在，而且是 `http://127.0.0.1`、`http://localhost` 或 `http://[::1]`，端口等于这块看板
- 正文超过 4096 字节返回 413
- 入库前再过一遍白名单。`tool_input` 之类即使被直接 POST 过来也不保存

内存里最多 200 条，多了就丢最旧的。快照里每个会话最多带最近 40 条，而且只带当前选中的 `session_id`。不写磁盘。

`/snapshot.json` 的 `live` 对象：

```json
{
  "v": 1,
  "source": "claude-hook",
  "merged": false,
  "correlate": "tool_use_id",
  "available": true,
  "fresh": true,
  "freshSeconds": 20,
  "events": []
}
```

`merged` 现在恒为 `false`。`correlate` 只是声明以后可以用 `tool_use_id` 和转录里的同一次工具调用对上，**这一版不做对账，也不会把两边收成一条**。动作流里会同时看到钩子行和转录行。

页面顶部的 **实时** 在最近 `freshSeconds` 秒内收到当前会话的事件时亮起；否则显示 **实时·等待**。这和原来的 LIVE（页面还连着看板）不是同一件事。成员卡片上的「实时 · …」也只覆盖显示，转录字段还在。

`python tools/demo_board.py` 的合成快照里有两条假事件，用来看这个指示。那不是本机钩子，横幅仍然写着合成数据。

## 还没做的事

事件和转录的去重合并是下一步，不是这次的范围。要对上的话，用 `session_id` + `tool_use_id` + `event`，不要用工具参数的文本。

## English

`python install.py --hooks` merges `PreToolUse`, `PostToolUse`, `SubagentStop`, `Notification`, and `Stop` command hooks into `~/.claude/settings.json`. Existing hook groups stay in place. A later `python install.py` without `--hooks` does not add or remove them. `python install.py --uninstall` removes only this tool's SessionStart hook and these relay hooks.

`team-board/hook_relay.py` reads the hook JSON from stdin, keeps the identifiers listed above, and POSTs them to `http://127.0.0.1:<port>/api/hooks/event` with a loopback `Origin`. The opener uses `ProxyHandler({})`, same as `ensure.py`. If the board is down the process exits 0 and prints nothing, so Claude Code does not treat the hook as a decision. The socket timeout is 0.2s; the settings `timeout` of 2 seconds is only Claude Code's ceiling.

The board accepts loopback clients only, requires a matching Origin, and rejects bodies over 4096 bytes. It stores at most 200 events in memory. `live.merged` is `false`: hook rows are not deduplicated against the transcript feed. `live.correlate` names `tool_use_id` as the future join key. That join is not implemented.
