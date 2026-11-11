# 解析性能基准

`tools/bench.py` 在临时目录里生成一套确定性的合成转录，调用看板真正的快照入口 `team_board.build_snapshot`，测量清空缓存后的耗时中位数和 tracemalloc 峰值。另外跑一段固定的纯 Python 校准循环。`--check` 比较的是「解析中位数 / 校准中位数」，不是绝对秒数。

它不读取 `~/.claude` 或 `~/.codex`，也不读本机消息总线。价格表固定用仓库里的 `team-board/team.json`。语料里的路径是 `/synthetic/bench-workspace`，没有家目录、邮箱或密钥形状的字符串。

## 量的是哪条路径

`build_snapshot` 是看板做快照时用的函数。默认语料有 10000 条 JSONL，种子是 29，放在一个临时 home 里：队长转录、四名成员，以及 `workflows/wave-1/` 下的一条子代理侧链。行里包含 user、assistant、`tool_use`、`tool_result`、usage、thinking、Bash 测试命令、`TaskUpdate`、`SendMessage`、派工、`isSidechain`、任务通知，以及一行无法解析的 `{`。

每次计入结果的运行之前会清空 `_agent_cache`、`_lead_cache`、`_conversation_cache` 和 `_lead_git_cache`。量的是文件变化之后的冷解析，不是命中缓存之后的快照。进程启动和模块导入不算在内。正式计时前有一次不计入的预热。默认重复 5 次，取中位数。

峰值内存来自另一次 `tracemalloc` 跟踪，是 Python 分配峰值，不是进程 RSS，也不决定通过或失败。

## 比值

校准循环默认跑 20000 次：反复 `json.loads` 同一行合成的 assistant / tool_use，并偶尔 `json.dumps`。机器整体变慢时，解析和校准会一起变。比值把这部分抵消掉，剩下的主要是解析代码本身相对这段固定工作的变化。

`--check` 读取 `benchmarks/baseline.json`。下面几项必须和基线一致，否则退出码是 2，并说明不一致，这不是性能告警：

- `schemaVersion`
- `entry`（`team_board.build_snapshot`）
- `events`
- `seed`
- `repeats`
- `calibrationLoops`
- `corpusSha256`

比值比基线慢超过 30% 时，退出码是 1，标准错误打印「告警」，并多打一行 GitHub Actions 的 `::warning`。更快，或慢但没超过 30%，退出码是 0。容差来自 `--threshold`，默认 `0.30`。基线文件里没有可以放宽 CI 的阈值字段。不要同时使用 `--check` 和 `--write-baseline`。

Python 的主版本或次版本和基线不同时，会多打印一行注意，然后仍然比较比值。当前基线是在 CPython 3.12.3 上记下的。

## 用法

在仓库根目录：

```bash
python tools/bench.py
python tools/bench.py --json
python tools/bench.py --check
python tools/bench.py --write-baseline
```

`--json` 只把结果写到标准输出。告警和「检查通过」写到标准错误。`--events` 最少 36、最多 100000。测试用小条数；默认和基线都是 10000。

`tests/test_bench.py` 在小语料上检查生成器两次写出的字节相同、结果字段，以及判定逻辑。它写入一份比值极小的假基线，确认 `--check` 以退出码 1 告警；再写入一份比值极大的假基线，确认更快时退出码为 0。因此告警不依赖某次 CI 真的变慢。

## 这次记下的数字

下面是 2026-11-11T02:34:21Z 在这台机器上执行 `python tools/bench.py --write-baseline` 时，程序打印出来的数。未再加工的浮点在 `benchmarks/baseline.json`。

环境：

- CPython 3.12.3
- `Linux-6.12.113+-x86_64-with-glibc2.39`
- `/proc/cpuinfo` 的 model name 是 `Intel(R) Xeon(R) Processor`，`nproc` 是 4。这是虚拟机报告的名字，不是具体型号。

结果：

| 项目 | 数值 |
| --- | --- |
| 事件 / 种子 / 重复 | 10000 / 29 / 5 |
| 解析中位数 | 0.3573 s |
| 解析样本 | 0.3568, 0.3538, 0.3586, 0.3573, 0.3574 |
| 校准中位数 | 0.0732 s（20000 次循环） |
| 校准样本 | 0.0715, 0.0758, 0.1117, 0.0732, 0.0728 |
| 比值 | 4.880642（文件里是 4.880641861195288） |
| tracemalloc 峰值 | 14620377 字节（约 13.94 MiB） |
| 快照 | 5 名成员，4125 次工具调用 |

紧接着的 `python tools/bench.py --check` 得到比值 4.782359，相对这份基线是 -2.0%，退出码 0。同一计时循环在这台机器上另外三次默认运行的比值是 4.8026、4.9438 和 5.0409。30% 容差高于这点抖动。

这些数字只描述这次记录。换机器或换 Python 版本都会变，也不是用量或账单。和基线比较时看比值，不看绝对秒数。

## 更新基线

只有解析实现变了、而且新的耗时是你打算接受的，才运行：

```bash
python tools/bench.py --write-baseline
```

提交前在说明里写上命令和环境。不要为了让 `--check` 通过而把比值改小，或把 `--threshold` 加大。生成器一变，语料 sha256 就变，`--check` 会因为协议不一致而失败，这时才需要连基线一起更新。

## CI

`.github/workflows/test.yml` 的 `bench` job 在 `ubuntu-latest`、Python 3.12 上运行 `python tools/bench.py --check`。超过容差时这个 job 失败。它不安装第三方包，也不挡住测试矩阵。
