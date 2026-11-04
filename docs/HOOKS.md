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
| `tool_use_id` | 同上。和转录并成一条时的首选键 |
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
  "merged": true,
  "correlate": "tool_use_id",
  "available": true,
  "fresh": true,
  "freshSeconds": 20,
  "events": []
}
```

`merged` 为 `true` 表示这帧快照的动作流已经和 `events` 对过账。`events` 仍是内存里的原始钩子（当前会话最近 40 条），用来判断通道是不是刚到过事件。动作流 `feed` 里同一次调用只有一条。

`correlate` 仍是 `tool_use_id`，这是首选键。完整规则写在 `team_board.activity_key` 上面：

1. 有 `tool_use_id`：键是 `session_id + tool_use_id`。`PreToolUse` 和 `PostToolUse` 算同一次。不看时间差，转录晚几秒写盘也对得上。两边工具名都有且不一样时不对上，只留转录那一条。
2. 没有 `tool_use_id`：键是 `session_id + tool_name + 5 秒窗口 + 入参指纹`。指纹是工具入参 JSON（键排序）的 sha256 前 16 位。钩子不转发 `tool_input`，所以钩子侧指纹为空；只有同一窗口里恰好一条同名转录时才并上。多条就不猜，也不另加一行。

并上之后保留更早的时间（通常是钩子先到的那一下），用转录补上 `工具 · 参数摘要`、测试结果和报错。`PostToolUse` 把这一条标成已完成。估算成本只来自转录里的 usage，钩子不加价、不记第二次 token。转录里已经数过的 id，`toolCount` 不加第二次；转录还没落盘时先算一次，落盘后回到转录的次数。

没装钩子时通道不可用，`merged` 保持 `false`，动作流和成本与只读转录时一样。装了钩子但这一帧还没有事件时，动作流也与只读转录相同，只是 `merged` 为 `true`，表示之后到的事件会并进去。

页面顶部的 **实时** 在最近 `freshSeconds` 秒内收到当前会话的事件时亮起；否则显示 **实时·等待**。这和原来的 LIVE（页面还连着看板）不是同一件事。成员卡片上的「实时 · …」只在这条调用还没写进转录时覆盖那一行；对上之后改回转录里的摘要。

`python tools/demo_board.py` 的合成快照里 `live.merged` 仍是 `false`，两条假事件只用来看指示，不经过这块对账。横幅仍然写着合成数据。

## 怎么自己对一下

用合成转录和合成钩子，不要把真实会话贴进来：

```bash
python -m unittest tests.test_event_merge tests.test_hook_relay -v
```

`tests/test_event_merge.py` 覆盖三种情况：先钩子后转录只有一条，而且时间留在钩子那边；只有转录时还是一条，成本不变；只有钩子时先出现一条，`PreToolUse` 加 `PostToolUse` 也不拆成两条。

## English

`python install.py --hooks` merges `PreToolUse`, `PostToolUse`, `SubagentStop`, `Notification`, and `Stop` command hooks into `~/.claude/settings.json`. Existing hook groups stay in place. A later `python install.py` without `--hooks` does not add or remove them. `python install.py --uninstall` removes only this tool's SessionStart hook and these relay hooks.

`team-board/hook_relay.py` reads the hook JSON from stdin, keeps the identifiers listed above, and POSTs them to `http://127.0.0.1:<port>/api/hooks/event` with a loopback `Origin`. The opener uses `ProxyHandler({})`, same as `ensure.py`. If the board is down the process exits 0 and prints nothing, so Claude Code does not treat the hook as a decision. The socket timeout is 0.2s; the settings `timeout` of 2 seconds is only Claude Code's ceiling.

The board accepts loopback clients only, requires a matching Origin, and rejects bodies over 4096 bytes. It stores at most 200 events in memory. `live.events` stays that raw ring. `live.merged` is true when those events have been joined into `feed`. `live.correlate` is still `tool_use_id`: the preferred key is `session_id + tool_use_id`, with no time limit. Without an id the key is `session_id + tool_name + a 5 second window + a sha256 prefix of the canonical tool input`. The relay still does not send tool input, so a hook fingerprint is empty and matches only a single transcript row in that window. The canonical row keeps the earlier timestamp and the transcript label. Usage and estimated cost stay on the transcript; a joined id is not counted twice. The static demo fixture leaves `merged` false and does not run this join.
