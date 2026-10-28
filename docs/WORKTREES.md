# Worktree 作战区

同一仓库可以同时开多路 Claude Code：主工作区一路，`claude --worktree <name>` 或 `git worktree add` 再各开一路。每路的转录落在不同的 `~/.claude/projects/<项目 key>/` 里，看板以前把它们看成互不相关的会话，也不显示分支。

现在快照里多了一个 `theaters` 数组。至少两个不同工作区的会话才收成一个作战区。顶栏任务条下面出现一行：仓库名，加上每个会话的分支。点分支会切到那一路会话。官方 Agent Teams 的任务看板和成员卡片不动。官方团队仍然不会把 teammate 放进各自的 worktree；这里分组的是多路会话，不是同一会话里的队友。

## 快照

`session` 和 `sessions[]` 各多三个字段：

| 字段 | 含义 |
|---|---|
| `branch` | 这一路会话的分支。没有就空字符串 |
| `worktree` | Claude 布局里的 worktree 名，或 git 管理目录名。主工作区是空字符串 |
| `theaterId` | 作战区 id。没有和别人组成作战区时是空字符串 |

`theaters[]` 的每一项是 `{id, label, sessions}`。`id` 是 `wt-` 加上仓库家族路径的 sha256 前 12 位。`label` 只保留目录名。`sessions` 里是 `id` / `project` / `branch` / `worktree` / `primary` / `mtime` / `count`。这里不放绝对路径。

主工作区排在前面，其余按最近活动时间。当前这 12 条会话之外、但属于作战区的会话会补进 `sessions`，下拉菜单才能切过去。

## 身份是怎么认的

只读本地已经有的信息，不运行 `git`，这样刷新看板不会触发仓库钩子。

1. **启动目录**。和看板原来的规则一样：队长转录里，项目 key 对得上转录所在目录的那个 `cwd`；对不上就用第一条 `cwd`。`claude --worktree` 是在工作区目录里启动的，所以启动目录就是那一个 worktree。
2. **转录字段 `gitBranch`**。每条记录上都可能有。值 `HEAD` 是占位，不当成分支名。只留最后一条像样的名字，当作退路。
3. **工作区上的 git 元数据**。沿启动目录往上找 `.git`：
   - 目录：主工作区，读 `.git/HEAD`
   - 文件：`gitdir:` 指向的管理目录。相对路径相对于这个文件所在的目录。管理目录里的 `commondir`（通常是 `../..`）或路径中的 `worktrees/<name>` 用来找到共用的 git 目录。分支读这个管理目录的 `HEAD`（`ref: refs/heads/<name>`）
4. **Claude 的默认目录**。路径里出现 `<repo>/.claude/worktrees/<name>/` 时，即使 `.git` 已经不在，也把 `<name>` 当成 worktree，并和 cwd 就是 `<repo>` 的会话放在一起。兄弟目录那种 `git worktree add ../somewhere` 没有这段路径，必须能读到 `.git` 文件才能归到同一仓库。

分支优先用第 3 步的 `HEAD`。Claude Code 会把 `gitBranch` 冻在会话开始时的值，人已经进了 worktree，转录里仍可能写着 `main`（见 [anthropics/claude-code#20406](https://github.com/anthropics/claude-code/issues/20406)）。`HEAD` 不是一条分支（分离 HEAD，或文件读不到）时，才用转录里的 `gitBranch`。

仓库家族：共用 git 目录的名字是 `.git` 时，用它的父目录（主工作区）；否则用共用 git 目录本身（例如 separate-git-dir）。家族路径只用来算 `theaterId`，不写进快照。

## 不做什么

- 没有子代理转录的会话，看板本来就不会列出。作战区也看不见它们。
- 同一工作区里的两个会话只是会话列表里的两行，不组成作战区。
- 启动目录在主工作区、后来才 `cd` 进另一个 worktree 的会话，仍算主工作区。并行的 `--worktree` 会话各自在自己的目录里启动，不受这条影响。
- 看板不创建、不删除 worktree，也不改分支。
- 只读演示 [`examples/board-demo.synthetic.json`](../examples/board-demo.synthetic.json) 里有一个合成作战区（`main` 和 `worktree-feature-board`）。点另一支分支只会改标签，成员卡片仍是那一组合成数据。可复制的 git 元数据在 [`examples/worktrees.synthetic/`](../examples/worktrees.synthetic/README.md)，测试复制到临时目录后再用。
