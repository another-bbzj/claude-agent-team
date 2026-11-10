# 发版检查清单

发版前先在仓库根目录跑一遍可重复的证据检查。`python tools/release_check.py` 只读仓库文件，不修改 `VERSION`，不打 tag，也不创建 GitHub Release。退出码 0 表示下面五项都通过；有一项失败则退出码为 1。`--json` 把同一结果写到标准输出，供脚本读取。

脚本变绿本身不是兼容性声明。某个系统或 Python 版本要写进变更记录，还得等本文后面的矩阵和用户配置检查也通过。

## 命令

```sh
python tools/release_check.py
python tools/release_check.py --json
```

人读的输出是一张表：检查名、通过或失败、一行说明，最后是「N 项通过，M 项失败」。`--json` 的对象是 `ok` 和 `checks`。每个检查有 `name`、`ok`、`detail`。`name` 与下表第一列相同。

| name | 表上的名字 | 通过标准 |
| --- | --- | --- |
| version | 版本号 | 根目录 `VERSION` 去掉空白后，与 `CHANGELOG.md` 里按文件顺序出现的第一个版本标题相同。标题形如 `## 1.4.3 — 2026-10-22`。`## 未发布` 不是版本段。后面更旧的 `## x.y.z` 不参与比较。 |
| unreleased | 未发布 | `## 未发布` 到下一个 `##` 之间有正文。只有小标题、空行、分隔线、HTML 注释，或没有文字的 `<details>` / `<summary>`，都算空。 |
| readme_commands | README 脚本 | `README.md` 至少引用一个仓库内脚本。`README.en.md` 若存在，其中的引用一并核对。`python` / `python3` / `python3.12` 后面的 `*.py`，以及反引号里的脚本路径，都必须能找到。路径里带 `/` 时按写出的位置找；只有文件名时依次看仓库根、`tools/`、`team-board/`。`python -m …`、以 `~` 开头的本机路径、带 `*` 的通配不要求对应文件。 |
| pages_site | 静态站 | 若存在 `tools/build_pages_site.py` 且源码里有 `--check`，就运行 `python tools/build_pages_site.py --check`，退出码必须为 0。没有这个脚本，或脚本没有 `--check` 时记为跳过，不算失败。本仓库有这个命令，跳过即说明检查没有真正跑到。 |
| tests_discoverable | 测试发现 | `tests/` 能被 `unittest` 以与 `python -m unittest discover -s tests` 相同的方式加载：至少 1 个测试，且没有导入错误。这里不执行测试，也不加载 `test*.py` 以外的辅助模块。 |

失败时按表里的说明改对应文件。不要为了让检查变绿而删掉「未发布」里已经写上的变更。

## 解释器和操作系统

支持范围以 [`.github/workflows/test.yml`](../.github/workflows/test.yml) 的测试矩阵为准，并且那次运行必须是绿的：

- Linux、macOS、Windows
- Python 3.12 与 3.13
- Python 3.8 只在 `ubuntu-24.04`。当前 Ubuntu 与 macOS 的默认托管镜像没有 3.8 的构建，所以 3.8 不在 `ubuntu-latest` 或 `macos-latest` 上跑

`release-check` 是同一个 workflow 里的单独 job。它不 `needs` 矩阵，矩阵也不 `needs` 它，因此不会挡住现有测试、lint 或覆盖率 job 的启动。它也不代替矩阵。矩阵没跑过的系统或版本，变更记录里不要写「支持」。

## 用户配置

安装和更新必须继续保留使用者改过的 hooks、`team.json`、成员、技能和导入的素材。证据是测试套件里的这些用例在矩阵里通过，而不是发版脚本再跑一遍安装：

- `tests/test_install.py`
- `tests/test_install_options.py`
- `tests/test_install_stability.py`
- `tests/test_hooks_install.py`
- `tests/test_update_stability.py`

这些用例使用临时家目录，不改维护者自己的 `~/.claude`。发版检查只确认 `tests/` 能被发现。安装测试被跳过或删掉时，不能把配置保留写成已验证。

## 变更记录

- 已发布的版本段和 `VERSION` 使用同一个号。
- 还没写进 `VERSION` 的变化留在「未发布」，并且这段不能空。
- 要把「未发布」收成新的 `## x.y.z — 日期` 时，同时改 `VERSION`，并在「未发布」留下其后的正文。只挪走、不留正文，这项检查会失败。
- 不写没有矩阵结果或对应用例支撑的兼容性、用量或集成结论。
- 本仓库目前没有 git tag。打 tag 或发布 GitHub Release 是维护者的另一步。这个脚本不做。

## 持续集成

`.github/workflows/test.yml` 的 `release-check` job 在 Ubuntu 上用 Python 3.12 运行 `python tools/release_check.py`。它失败时，这次发版证据是红的；矩阵 job 仍会照常启动。
