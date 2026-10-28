# 合成的 git worktree 目录

这棵目录是假数据，给 `tests/test_worktrees.py` 复制到临时目录后用。不要把它放进真实的 `~/.claude`，也不要在这里执行 `git`。

`repo/dot-git` 复制后改名为 `.git`，`repo/dot-claude` 改名为 `.claude`，`gitfile` 改名为工作区里的 `.git` 文件。这样仓库里不会嵌一套真的 git 目录。

```
repo/dot-git/HEAD                                      主工作区，分支 main
repo/dot-git/worktrees/feature-board/HEAD              分支 worktree-feature-board
repo/dot-git/worktrees/feature-board/commondir         ../..  → 主仓库的 .git
repo/dot-claude/worktrees/feature-board/gitfile        相对 gitdir，指向上面的 worktree 管理目录
```

这是 Claude Code `claude --worktree feature-board` 的默认布局：工作区在 `<repo>/.claude/worktrees/<name>/`，分支名通常是 `worktree-<name>`。`gitfile` 用相对路径，已提交文件里没有绝对路径。

看板怎么把两个会话收成一个作战区，写在 [`docs/WORKTREES.md`](../../docs/WORKTREES.md)。
