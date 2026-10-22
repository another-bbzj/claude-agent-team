# 变更记录

仓库**没有 git tag**。下面的版本号来自提交说明和根目录 [`VERSION`](VERSION)（当前 `1.4.3`）。日期是该版本所记变化合入 `main` 的作者日期。`1.4.3` 记到 2026-10-22，含只读详情和筛选芯片。未合并到 `main` 的分支不记在这里。

每节先写使用者能看到的变化，折叠块里是对应区间的非合并提交，方便对照，不是另一份发布说明。

## 1.4.3 — 2026-10-22

选中会话对得上 `~/.claude/tasks/session-<前 8 位>/` 时，部门汇总下面多一行只读官方任务看板。三列是待处理、进行中、已完成。卡片不能拖，也不能把状态写回任务目录。

- 悬停或聚焦时，只有 `owner` 与成员名字或 lead 名字完全一致，看板卡和舞台上的人才互相点亮。没认领或对不上的任务保持原样，其他人的任务变淡。
- 普通点击，或聚焦后按 Enter / 空格，打开只读详情：完整说明、状态、负责人、依赖和挡住的任务（对得上的带标题）、停留时间，以及阻塞、大约计时这类标记。Esc、点外面或关闭按钮都会关掉。
- 看板卡用 Alt+点击或卡片上的「钉」钉住高亮；点舞台上成员名字一带也可以钉住（点形象仍然打开成员抽屉）。再操作一次取消。Esc 先关弹窗和任务详情，再取消钉住。
- 标题下可以按全部、阻塞中、未对上人、已对上筛选。筛选只藏卡片，不改任务目录，也不改标题上的待处理 / 进行中 / 已完成数量。选择记在这个标签页的 `sessionStorage` 里，按会话 id 和团队名分开。钉住的名字如果当前筛选下一张卡都看不见，就取消钉住。
- 隐私安全的合成样例在 `examples/agent-teams.synthetic/`。`python tools/demo_board.py` 用 `examples/board-demo.synthetic.json` 展示同一套只读看板、详情和筛选，不读本机 `~/.claude/tasks/`。示意图是 `docs/img/task-board.svg`，没有画出筛选按钮。

这些能力来自已合入的 #18、#19、#23、#26、#27、#28、#29、#30。`1.4.2` 之后、这次改 `VERSION` 之前，`main` 上还合入了 #4、#5、#6、#7、#8、#9、#10、#11、#12、#13，记在下面，避免版本号跳过它们。

### 修复

- 安装器在合并 `SessionStart` 时保留同一分组里已有的用户钩子（[`78953de`](https://github.com/another-bbzj/claude-agent-team/commit/78953de)）。`--petdex` / `--no-pets` 和是否重启看板不再绑在一起（[`6ae97ff`](https://github.com/another-bbzj/claude-agent-team/commit/6ae97ff)）。
- 看板拒绝不合法的请求体，并继续使用上一份有效配置（[`a8827d0`](https://github.com/another-bbzj/claude-agent-team/commit/a8827d0)）。回环地址开始监听之前不再做反向 DNS（[`f633846`](https://github.com/another-bbzj/claude-agent-team/commit/f633846)）。
- 更新脚本在新进程没有起来时报告失败，并清掉临时文件（[`3330767`](https://github.com/another-bbzj/claude-agent-team/commit/3330767)）。
- `ensure.py` 查找过期看板进程时不再调用 `lsof`，并去掉 `re.split` 的 maxsplit 警告（[`17d0443`](https://github.com/another-bbzj/claude-agent-team/commit/17d0443)，#9）。

### 新增

- `tools/export_snapshot.py`：按数字字段白名单导出聚合指标，不带提示词、正文、会话标识或本机路径（[`ecf9802`](https://github.com/another-bbzj/claude-agent-team/commit/ecf9802)）。
- `tools/codex_metrics.py`：只汇总你指定的 `codex exec --json` 日志，不启动 Codex，也不读取交互会话（[`cb52209`](https://github.com/another-bbzj/claude-agent-team/commit/cb52209)）。
- `tools/demo_board.py`：用仓库里的合成快照起一个只读看板，不读 `~/.claude` 或 `~/.codex`（[`0f4bbc7`](https://github.com/another-bbzj/claude-agent-team/commit/0f4bbc7)）。官方任务行是后来补进这份快照的，见本节开头。

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
<summary>官方任务看板相关的非合并提交</summary>

- `6a5cbd3` docs(agent-teams): record official on-disk layout and synthetic fixtures (#18)
- `26ebc84` 解析官方 Agent Teams 的成员与任务状态 (#19)
- `b251930` 看板显示官方 Agent Team 成员与「官方 Team」徽章 (#23)
- `ab1e63c` 只读展示官方 Agent Teams 任务看板 (#26)
- `233fcd6` 扩充官方 Agent Teams 合成任务 fixture 并加强隐私护栏 (#27)
- `e6f6499` 官方任务卡和成员卡按完全相同的名字互相点亮 (#28)
- `6c16f93` 官方任务卡片打开只读详情 (#29)
- `46f851a` 官方任务看板按阻塞和认领情况筛选 (#30)

</details>

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
