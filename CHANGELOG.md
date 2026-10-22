# 变更记录

仓库**没有 git tag**。下面的版本号来自提交说明和根目录 [`VERSION`](VERSION)（当前 `1.4.2`）。日期是该提交在 `main` 上的作者日期。未合并到 `main` 的分支不记在这里。

每节先写使用者能看到的变化，折叠块里是对应区间的非合并提交，方便对照，不是另一份发布说明。

## 未发布

`1.4.2` 于 2026-09-25 合入 `main`（[`a3acd71`](https://github.com/another-bbzj/claude-agent-team/commit/a3acd71)）之后，2026-10-21 又合入了 #4、#5、#6、#7、#8、#9、#10、#11、#12、#13。`VERSION` 没有因此改号。

### 修复

- 安装器在合并 `SessionStart` 时保留同一分组里已有的用户钩子（[`78953de`](https://github.com/another-bbzj/claude-agent-team/commit/78953de)）。`--petdex` / `--no-pets` 和是否重启看板不再绑在一起（[`6ae97ff`](https://github.com/another-bbzj/claude-agent-team/commit/6ae97ff)）。
- 看板拒绝不合法的请求体，并继续使用上一份有效配置（[`a8827d0`](https://github.com/another-bbzj/claude-agent-team/commit/a8827d0)）。回环地址开始监听之前不再做反向 DNS（[`f633846`](https://github.com/another-bbzj/claude-agent-team/commit/f633846)）。
- 更新脚本在新进程没有起来时报告失败，并清掉临时文件（[`3330767`](https://github.com/another-bbzj/claude-agent-team/commit/3330767)）。
- `ensure.py` 查找过期看板进程时不再调用 `lsof`，并去掉 `re.split` 的 maxsplit 警告（[`17d0443`](https://github.com/another-bbzj/claude-agent-team/commit/17d0443)，#9）。

### 新增

- 同一 git 仓库在不同 worktree 里的多路会话收成一个作战区，每路显示自己的分支。任务条下方可以点分支切换。说明在 `docs/WORKTREES.md`。
- 官方任务看板的卡片可以点开只读详情：标题、完整说明、状态、负责人、依赖和挡住的任务（对得上的带标题）、在当前状态停留的时间，以及阻塞或大约计时这类标记。普通点击和聚焦后的 Enter / 空格打开详情，不钉住高亮。Esc、点外面或关闭按钮都会关掉。仍然不能改 `~/.claude/tasks/`。
- 官方任务看板和舞台上的成员可以按完全相同的名字互相点亮。悬停或聚焦看板卡会点亮对应的人；悬停或聚焦成员会点亮他名下的任务。没认领，或 owner 对不上名字的任务保持原样，其他人的任务变淡。点舞台上成员名字一带可以钉住；看板卡用 Alt+点击或卡片上的「钉」钉住，再操作一次取消。Esc 先关弹窗和任务详情，再取消钉住。仍然只读，跟着原来的 `/snapshot.json` 刷新。
- `tools/export_snapshot.py`：按数字字段白名单导出聚合指标，不带提示词、正文、会话标识或本机路径（[`ecf9802`](https://github.com/another-bbzj/claude-agent-team/commit/ecf9802)）。
- `tools/codex_metrics.py`：只汇总你指定的 `codex exec --json` 日志，不启动 Codex，也不读取交互会话（[`cb52209`](https://github.com/another-bbzj/claude-agent-team/commit/cb52209)）。
- `tools/demo_board.py`：用仓库里的合成快照起一个只读看板，不读 `~/.claude` 或 `~/.codex`（[`0f4bbc7`](https://github.com/another-bbzj/claude-agent-team/commit/0f4bbc7)）。

### 文档

- 贡献说明、分诊路径和私密安全报告（[`6d82022`](https://github.com/another-bbzj/claude-agent-team/commit/6d82022)）。
- Codex 维护说明和生态路线图（[`87cb76d`](https://github.com/another-bbzj/claude-agent-team/commit/87cb76d)）。
- 把离线指标和免账号演示写进现有文档（[`a5dbbde`](https://github.com/another-bbzj/claude-agent-team/commit/a5dbbde)）。
- 中英文首页收成首屏：是什么、为什么、30 秒开始。安装、协作、通信、形象、估价和常见问题进了指南，测试数量与 `tests/` 里的 `test_*` 一致（[`1a8b0d0`](https://github.com/another-bbzj/claude-agent-team/commit/1a8b0d0)，#10）。

### 持续集成

- CI 启用 ruff（pyflakes、语法错误、bugbear），并修掉它报出的未使用导入和循环变量迟绑定（[`a07d75f`](https://github.com/another-bbzj/claude-agent-team/commit/a07d75f)，#12）。
- 测试矩阵加上 Python 3.13；Ubuntu 上卸掉 `lsof` 再跑整套测试；pip 缓存键改为 `requirements-dev.txt`（[`ed76cf5`](https://github.com/another-bbzj/claude-agent-team/commit/ed76cf5)，#11）。

### 测试

- `fetch_petdex.py` 补上清单缓存和失败路径；该文件覆盖率低于 95% 时 CI 失败（[`a14571c`](https://github.com/another-bbzj/claude-agent-team/commit/a14571c)，#13）。

<details>
<summary>2026-10-21 的非合并提交</summary>

- `ed76cf5` ci: test Python 3.13 and Linux without lsof (#11)
- `a14571c` test: cover petdex selection, cache and install errors (#13)
- `1a8b0d0` docs: turn the READMEs into a first screen (#10)
- `a07d75f` ci: check the tree with ruff (#12)
- `17d0443` fix(ensure): find stale board PID without lsof; silence re.split maxsplit warning (#9)
- `78953de` fix(installer): preserve user hooks in shared session groups
- `a8827d0` fix(board): reject invalid requests and retain valid configuration
- `3330767` fix(updater): report startup failures and clean temporary files
- `8611e90` test(ensure): include startup exit status and service logs in failures
- `f633846` fix(board): avoid reverse DNS before loopback server listens
- `6d82022` docs(community): add contribution, triage and private security paths
- `ecf9802` feat(integrations): export privacy-safe aggregate snapshot metrics
- `87cb76d` docs(codex): document maintainer workflows and ecosystem roadmap
- `dc06953` test(board): verify listening with a portable loopback connection
- `6ae97ff` fix(installer): keep import options independent from restart controls
- `cb52209` feat(codex): summarize explicit exec JSONL captures offline
- `0f4bbc7` feat(demo): provide an isolated read-only synthetic board
- `a5dbbde` docs(ecosystem): connect Codex metrics and no-account demo guides

</details>

## 1.4.2 — 2026-09-25

Wallpaper Engine 的每一项都能应用；3D 场景改用场景包里的全分辨率原图（[`63101a6`](https://github.com/another-bbzj/claude-agent-team/commit/63101a6)）。外观设置改为攒齐防抖补丁再保存，避免只留下最后一次滑动（[`e5174b1`](https://github.com/another-bbzj/claude-agent-team/commit/e5174b1)）。消息总线按真实路径解析项目，符号链接目录（包括 macOS 上 `/var` → `/private/var`）不再对不上（[`5250eb5`](https://github.com/another-bbzj/claude-agent-team/commit/5250eb5)）。舞台面板的透明度跟着面板滑块走（[`2282147`](https://github.com/another-bbzj/claude-agent-team/commit/2282147)）。这些提交经 #1 合入 `main`。

## 1.4.1 — 2026-09-25

去掉内置的 Shu 预设。顶栏可以直接调透明度。外观面板改为中文（[`29055c5`](https://github.com/another-bbzj/claude-agent-team/commit/29055c5)）。

## 1.4.0 — 2026-09-25

罗德岛终端风格的界面、成员之间的消息总线，以及 Wallpaper Engine 背景板（[`5400a28`](https://github.com/another-bbzj/claude-agent-team/commit/5400a28)）。

## 1.3.3 — 2026-09-19

导入形象不再静默失败：独立对话框，支持拖放、同时选 `pet.json` 和雪碧图、填写本机路径；中文名可以做显示名。修了控制台编码把进程打崩的问题（[`df732eb`](https://github.com/another-bbzj/claude-agent-team/commit/df732eb)）。

## 1.3.2 — 2026-09-18

`ensure.py` 发现正在跑的看板和本机 `VERSION` 不一致时会换掉旧进程，避免升级后导入形象报 `not found`。`install.py` 装完会重启看板。启动时扫描 `sprites/`、`~/.codex/pets/`、`~/.petdex/pets/`。顶栏有版本号和「检查更新」（[`e01d959`](https://github.com/another-bbzj/claude-agent-team/commit/e01d959)）。

## 1.3.1 — 2026-09-18

按每个动作行的不透明质量和包围盒适配桌宠主体：改得下的写回图片并留 `.orig`，其余记在 `fitRows`，被裁切的行不动（[`0192a99`](https://github.com/another-bbzj/claude-agent-team/commit/0192a99)）。在此之前，9 行自定义雪碧图改为按宽度算背景尺寸，偏小的角色在屏幕上放大（[`1d05901`](https://github.com/another-bbzj/claude-agent-team/commit/1d05901)）；详情抽屉的形象选择不再被 1.5 秒刷新冲掉（[`8f4c0c5`](https://github.com/another-bbzj/claude-agent-team/commit/8f4c0c5)）。

## 1.3.0 — 2026-09-18

工作中的成员和队长可以在抽屉里换形象。一键更新（`VERSION`、`update.py`、`/api/update`）。第三方模型 id、provider 前缀和按 requestId 去重；价格表扩展到提交说明里的 50 多行。安装和更新不覆盖你改过的 `team.json`、成员文件和技能（`*.new` 与 `dist-hashes`）（[`ebc1cac`](https://github.com/another-bbzj/claude-agent-team/commit/ebc1cac)）。

同一天先落下来的还有：任意桌宠文件导入、petdex 下载，以及编辑成员时保留表单不管的 frontmatter（[`f7e8c7c`](https://github.com/another-bbzj/claude-agent-team/commit/f7e8c7c)）；空部门显示「按需出场」，收尾后才用黄色「建议补位」，临时成员用「部门名·标题」归入部门（[`25eac5e`](https://github.com/another-bbzj/claude-agent-team/commit/25eac5e)）。

## 0.1 — 2026-09-17

第一版：分部门的常驻 Agent 和本机实时看板（[`e4a21d0`](https://github.com/another-bbzj/claude-agent-team/commit/e4a21d0)）。同一天补上了队长自己的用量和成本、图文手册、标准库测试和三个系统的 CI、按 message id 去重的 token 统计，以及 DeepSeek / OpenAI / Gemini / Qwen / GLM / Kimi / MiniMax 的最长前缀标价（[`0ed7b53`](https://github.com/another-bbzj/claude-agent-team/commit/0ed7b53)、[`3f30175`](https://github.com/another-bbzj/claude-agent-team/commit/3f30175)、[`9f76f3b`](https://github.com/another-bbzj/claude-agent-team/commit/9f76f3b)、[`bca4058`](https://github.com/another-bbzj/claude-agent-team/commit/bca4058)、[`6c90cdd`](https://github.com/another-bbzj/claude-agent-team/commit/6c90cdd)）。仓库自带 9 只程序绘制的形象；Codex 自带的 9 只只从你本机安装里导入，不放进仓库（[`bb856e2`](https://github.com/another-bbzj/claude-agent-team/commit/bb856e2)、[`bb1e585`](https://github.com/another-bbzj/claude-agent-team/commit/bb1e585)）。

<details>
<summary>2026-09-17 的非合并提交</summary>

- `e4a21d0` claude-agent-team v0.1: department-based agent team + live board
- `f529203` Fix stats table overflow and equal-height lower panels
- `f13185d` Pet lookup falls back to sprites actually present
- `bb1e585` Import the 9 Codex pets from the user's own install (no redistribution)
- `ee2ad3b` README: step-by-step guide
- `3f30175` Illustrated guide and per-department comparison bars
- `0ed7b53` Stats include the lead and all-team totals
- `bb856e2` Ship 9 original generated pets
- `3623b06` Roster cards: wider min width
- `9f76f3b` Test suite and CI on 3 OSes
- `9054dfb` README: CI status badge
- `bca4058` Fix token double-counting; no invented third-party prices
- `6c90cdd` Built-in pricing for DeepSeek/OpenAI/Gemini/Qwen/GLM/Kimi/MiniMax

</details>
