# 常见问题

这些问题都来自指南的 [常见问题](GUIDE.md#faq)。2026-10-21 查看本仓库的公开 issue 列表时是空的，所以没有从 issue 再收问题，也没有新编问题。下面按主题重排，答案保持原来的说法。

English: this page restates the FAQ in [GUIDE.md](GUIDE.md#faq). The public issue list was empty when it was collected, so no issue thread was added and no new question was invented.

## 安装和更新

**`git pull && python install.py` 之后，导入形象提示 `not found`，顶栏没有版本号和「检查更新」，或者文件夹里的形象没出现。**

之前启动的看板进程还在跑旧代码（页面已经是新的，服务还是旧的）。1.3.2 起：`install.py` 装完会自动重启看板；`ensure.py`（SessionStart 钩子）每次都会比对在跑的服务版本和本机 `VERSION`，不一致就自动换掉。现在手动跑一次 `python ~/.claude/team-board/ensure.py --restart` 即可。顶栏常驻 `v1.x.x` 版本号和「检查更新」按钮，有新版本会变成「⬆ 一键更新」。

An old board process is still serving the previous code. Since 1.3.2, `install.py` restarts the board, and `ensure.py` replaces it when the running build does not match `VERSION`. Run `python ~/.claude/team-board/ensure.py --restart` once.

**导入形象没反应，中文名被说成「标识只能用小写字母」，或者没有 `.zip` 后缀的压缩包选不到。**

1.3.3 起这些都不会再挡住你：导入走一个专门的对话框（不再靠浏览器的 `prompt` 弹窗——嵌入式浏览器里它弹不出来，之前抽屉里导入出错也没地方显示），成功和失败都写在对话框里；中文名记成显示名、标识自动从文件名或 `pet.json` 推，推不出就 `pet-1`；文件按内容识别不看后缀；还可以直接填本机路径（比如 `C:\Users\你\Downloads\zip`），看板自己去读。

Since 1.3.3 the importer is its own dialog. A Chinese name is the display name. The id is derived from the filename or `pet.json`, or falls back to `pet-1`. Type is sniffed from the bytes. A local path works.

**把桌宠文件直接放进 `sprites/`、`~/.codex/pets/` 或 `~/.petdex/pets/` 之后，怎么让看板认到？**

不用做什么：服务启动时和每次打开成员表单或形象列表时会自动扫描。大写、下划线、`xxx-spritesheet.webp` 这类文件名会复制成合法 id（`My_Cat-spritesheet.webp` → `my-cat`）。`pet.json` 加 `spritesheet.webp` 的目录会连名字和作者一起纳入。

The board scans those directories on startup and when the member form or sprite list opens.

## 看板打不开

**看板打不开，或页面空白。**

新开一个会话让钩子触发，或手动 `python ~/.claude/team-board/ensure.py --open`。端口被占用可改 `TEAM_BOARD_PORT`。

Start a new session, or run `ensure.py --open`. Set `TEAM_BOARD_PORT` if the port is taken.

**说了「用 agent team」但看板没自动弹出来。macOS 和纯 CLI 上比较常见。**

钩子只负责让服务在后台常驻。弹窗由技能在派工前执行 `ensure.py --open` 完成（系统默认浏览器），同时队长会在回复里给出可点击的 **Agent Team 指挥室** 链接。如果它忘了，直接说「打开看板」，或自己开 <http://127.0.0.1:7788/>。服务一直在。

The hook only keeps the service up. The skill is supposed to open the browser and the lead should paste the board link. The service is already on loopback if you open the URL yourself.

**看板显示「还没有会话派出过子代理」。**

正常。你还没派过团队。派一次后自动出现。右上角下拉可切换到任何历史会话回看。

Expected until the first team dispatch. The session menu can open an older one.

**压缩了上下文（`/compact`）之后，看板数据会丢吗？**

不会。看板读的是转录文件。压缩只影响模型看到的上下文，文件里的历史都在。成员、通信、token 统计照常。

No. Compaction changes the model’s context, not the transcript file the board reads.

## 成员和部门

**新建的成员派不出去。**

成员在会话启动时加载，新开一个会话即可。临时要用，让队长派 `general-purpose` 并把标题写成「部门名·任务」。

Definitions load when a session starts. For an immediate job, dispatch `general-purpose` with a title `部门名·任务`.

**Claude 没开团队，自己做了。**

它判断为 S 级（不超过 2 个文件）。明确说「就用团队做」它会照办。

It classified the task as S. Say to use the team anyway.

**某个部门没人出场，看板会报错吗？**

不会。进行中一律显示「按需出场」。全员交付后，只有至少 2 人写了代码却没有质量部，或交付给人用却没有文档部时，才用黄色提示「建议补位」。

No. Empty departments say “as needed” while work is in progress. Amber “consider filling this seat” appears only after delivery, and only for those two cases.

## 通信

**子代理之间怎么通信？有人说子代理没有私信工具。**

原生 `SendMessage` 能用最好，但不是所有环境都支持。

1. 成员在 `.team/inbox/` 里写留言，队长负责转达。
2. 用 `msg.py`（推荐）：`python ~/.claude/team-board/msg.py send backend-dev "数据库已完成"`。任何能跑 Bash 的环境都行。
3. 看板上的「通讯频道」汇聚这些消息，你也能在那里直接回复。详见指南的 [成员之间通信](GUIDE.md#msg)。

Prefer native `SendMessage` when it exists. Otherwise write `.team/inbox/`, or use `msg.py`. The board’s comms panel collects both.

## 壁纸

**Wallpaper Engine 页签是空的，3D 场景很糊，或视频没动。**

- 空的：看板没找到 Steam 库。把壁纸目录（`…/steamapps/workshop/content/431960` 或其所在的 Steam 库）写进 `WALLPAPER_ENGINE_DIRS`（多个用 `;` 分隔），重启看板。
- 糊：3D 场景是 Wallpaper Engine 的私有格式。看板会先从场景包里找嵌着的原图。角标是「仅预览」说明这个场景只有 GPU 纹理、没有可提取的原图。先在 Wallpaper Engine 里把它设为桌面，再在外观面板用「当前桌面」拿全分辨率快照。
- 视频黑屏或自动变成静态图：浏览器解不了这个视频（常见于 HEVC / H.265 没装扩展，或 8K 这类超大分辨率）。看板已改用预览图。能不能放，以浏览器实际结果为准。

Point `WALLPAPER_ENGINE_DIRS` at the workshop folder if the list is empty. A blurry 3D scene has no extractable still; set it as the desktop and use “current desktop”. Video that the browser cannot decode falls back to the preview.

## 数字、卸载、换电脑

**token 或成本和服务商后台对不上。**

- 看板的「合计」是输入 + 输出 + 缓存读 + 缓存写。缓存读通常占 90% 以上。服务商后台可能只显示输入 + 输出，对比时要看同一口径。
- 成本按 `team-board/team.json` 的公开单价估算，不是账单。

The board’s total includes cache read and cache write. A console that shows only input and output is a different sum. Cost is the public price table, not an invoice.

**卸载会删掉什么？**

`python install.py --uninstall` 删除 `~/.claude/team-board/` 整个目录，包括消息总线记录 `bus/` 和背景图 `backdrops/`。`settings.json` 与 `CLAUDE.md` 只移除本工具加入的部分。更新（重装）不会动这两个目录。

Uninstall removes the whole `~/.claude/team-board/` directory, including `bus/` and `backdrops/`. An update does not.

**换电脑。**

把仓库 clone 过去再 `python install.py`。钩子路径按新机器自动生成。

Clone again and run `python install.py`. Hook paths are generated for that machine.
