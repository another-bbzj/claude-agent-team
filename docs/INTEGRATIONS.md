# Integration recipes

These recipes use the existing Claude Code board and standard-library tools. They do not add a hosted service or interactive Codex session ingestion. See [the main guide](GUIDE.md) for board setup and [the Codex maintainer guide](CODEX.md) for using Codex to maintain this repository.

## Optional Claude Code hook channel

`python install.py --hooks` registers a local command hook. It does not replace hooks you already configured. The relay posts a short JSON event to `POST /api/hooks/event` on `127.0.0.1`. The snapshot field `live` lists those events for the selected session and sets `merged` to false: they are not deduplicated against transcript rows. `correlate` is `tool_use_id`, reserved for a later join. Full field list, failure behavior, and uninstall notes are in [HOOKS.md](HOOKS.md).

## Codex exec event metrics

The [Codex exec adapter](CODEX_METRICS.md) accepts an explicitly supplied `codex exec --json` log and emits aggregate counts and token usage without text, commands or identifiers:

```sh
python tools/codex_metrics.py --input examples/codex-exec.synthetic.jsonl
```

This command works entirely offline and does not start Codex or spend API credits. Its event-report format is separate from the board snapshot export below. For a no-account UI walkthrough, use the [read-only synthetic demo](DEMO.md).

## Export aggregate metrics

From the repository root, try the entirely synthetic fixture without installing or starting the board:

```sh
python tools/export_snapshot.py --input examples/snapshot.synthetic.json
python tools/export_snapshot.py --input examples/snapshot.synthetic.json --output metrics.json
```

To read an already-running board:

```sh
python tools/export_snapshot.py --port 7788 --output metrics.json
```

When several projects are active, select the source explicitly. The same selectors work with the default JSON output and with `--format prometheus`:

```sh
python tools/export_snapshot.py --port 7788 --session sess-0001 --output metrics.json
python tools/export_snapshot.py --port 7788 --project C--demo --format prometheus --output team.prom
```

`--session` matches a session ID prefix. `--project` prefers an exact project key, otherwise a substring; it selects the most recently active matching session. A session prefix shared by multiple sessions also selects the most recently active match, so use a full ID when you need a unique target. Project keys are the keys shown by the board, not filesystem paths. These mutually exclusive options require `--port`; they cannot filter an offline `--input` file. Omitting them preserves the server's default selection. Browser session selection is independent of this command.

The exporter URL-encodes the selector and verifies the returned session metadata before rendering JSON or Prometheus text. Missing or mismatched session metadata returns 1 and leaves any existing output file unchanged. The file is replaced atomically only after a successful read and render. Selector values remain local to the request and never appear in exported aggregates, Prometheus labels, or fixed error diagnostics. The JSON output schema remains version 1.

Python 3.8+ and its standard library are sufficient. Choose exactly one of `--input` or `--port`. The output directory must already exist. The exporter refuses an output path that resolves to the input file, including an existing hardlink alias. Exit code 0 means success; 1 means a read, validation or write failure; argparse uses 2 for invalid command-line arguments. Error messages omit source data and tracebacks.

The live mode performs one read-only GET to `http://127.0.0.1:<port>/snapshot.json` with an optional encoded `session` or `project` query parameter, bypasses environment/system HTTP proxies, refuses all redirects, and has a five-second socket timeout. Both file and HTTP inputs have an 8 MiB limit. There is no arbitrary remote-URL option or automatic upload.

### Output contract: schemaVersion 1

The exporter constructs a new object from this explicit allowlist:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | Export format version, currently 1 |
| `memberCount` | Number of member entries in the selected board snapshot; excludes the lead |
| `totals.cost` | Estimated USD cost for lead and members |
| `totals.membersCost` | Estimated USD cost for members only |
| `totals.unpriced` | Count of lead/member entries with no configured price |
| `totals.tokens` | Member output tokens (not all tokens) |
| `totals.usage` | Aggregate member usage |
| `totals.leadUsage` | Aggregate lead usage |
| `totals.allUsage` | Combined lead and member usage |

Each usage object contains only `in`, `out`, `cache_read`, `cache_write`, `total`, and `cacheHit`. Token/count values must be nonnegative integers; costs must be finite nonnegative numbers; `cacheHit` must be between 0 and 1. Booleans are not accepted as numbers. Costs use the board's configured estimates, not provider invoices. With unpriced models, cost is a partial estimate; check `unpriced` before interpreting it.

The source board JSON is an internal interface that can evolve. The exporter validates the fields it needs and rejects unsupported shapes rather than silently returning zero metrics. Unknown source fields are ignored. Consumers should depend on the versioned exporter output and test against `examples/snapshot.synthetic.json`, rather than copy the board's raw snapshot schema.

## Prometheus and node_exporter

The same exporter supports the [Prometheus text format](https://prometheus.io/docs/instrumenting/exposition_formats/) with `--format prometheus`. Python 3.8+ standard-library modules are sufficient; Prometheus and node_exporter are optional consumers installed separately. JSON remains the default.

Try the entire conversion offline, without an account or a running board:

```sh
python tools/export_snapshot.py --input examples/snapshot.synthetic.json --format prometheus
python tools/export_snapshot.py --input examples/snapshot.synthetic.json --format prometheus --output team.prom
```

Compare the output with [`examples/prometheus.synthetic.prom`](../examples/prometheus.synthetic.prom). The fixture contains five metric families and 22 samples, all derived from the existing synthetic snapshot. That fixture has no session budget, so it does not include `claude_agent_team_budget_utilization_ratio`. The series appears only when `budget.enabled` is true and `budget.ratio` is a finite nonnegative number (known spend divided by the session cap, which may exceed 1). It is not part of the JSON schemaVersion 1 object. The same fixture has no parsed test result, so it also omits `claude_agent_team_test_results`. That family appears only when at least one member `lastTest.status` is `pass`, `fail` or `unknown`. Task copies are not counted again. It is not part of the JSON schemaVersion 1 object.

### Metric contract

| Metric | Labels | Meaning |
| --- | --- | --- |
| `claude_agent_team_members` | none | Number of member entries; excludes the lead |
| `claude_agent_team_estimated_cost_usd` | `scope="all"` or `"members"` | Estimated USD cost; partial when entries are unpriced |
| `claude_agent_team_unpriced_entries` | none | Lead/member entries without a configured price |
| `claude_agent_team_tokens` | `scope="members"`, `"lead"`, `"all"`; `kind="input"`, `"output"`, `"cache_read"`, `"cache_write"`, `"total"` | Board-reported token buckets for each scope |
| `claude_agent_team_cache_hit_ratio` | `scope="members"`, `"lead"`, `"all"` | Board-reported cache hit ratio between 0 and 1 |
| `claude_agent_team_budget_utilization_ratio` | `scope="session"` | Optional. Known session spend divided by the configured USD cap. Omitted when no session cap is set. May exceed 1 |
| `claude_agent_team_test_results` | `status="pass"`, `"fail"` or `"unknown"` | Optional. Latest parsed test per member. Omitted when no member has one of those statuses. Counts can fall when the selection changes |

Every family includes `HELP` and `TYPE` metadata. **All metrics are gauges** for the board's currently selected session, not lifetime counters: selecting another session or revising prices may lower a value. Use direct gauge queries, not `rate()` or `increase()`. Scopes overlap (`all` includes lead and members), and token `total` overlaps its component buckets. Select one scope and kind rather than summing across every sample. Costs are estimates, not provider invoices.

Live mode reads one session through `/snapshot.json`. Without `--session` or `--project`, that is the server's default session, usually the most recent one unless the board was started with a session or project filter. To publish a different project as a Prometheus textfile:

```sh
python tools/export_snapshot.py --port 7788 --project C--demo --format prometheus --output team.prom
```

Changing the browser's session selector does not change this command's source. The output is one selected session, not an aggregate of every session. Use an explicit offline snapshot when you need to control the source independently of the live server.

The format has fixed metric names and fixed label values. No session IDs, member/model names, paths, messages or raw errors become labels or help text. There are no per-sample timestamps. The file and live input modes retain the JSON exporter's 8 MiB limit, validation, proxy bypass and redirect refusal. Prometheus samples must also fit a finite float64 value; excessively large JSON integers are rejected. Counts above float64's exact integer precision can be rounded by the monitoring system.

### Collect a local board through node_exporter

On a host with an existing [node_exporter textfile collector](https://github.com/prometheus/node_exporter#textfile-collector), create a writable collector directory accessible to the account that runs the exporter command and readable by node_exporter. For a local trial, run both commands as the same account in separate terminals:

```sh
mkdir -p /tmp/team-metrics
# From the repository root, with the board already running.
# Omit --project to keep the server's default session.
python tools/export_snapshot.py --port 7788 --project C--demo --format prometheus --output /tmp/team-metrics/claude_agent_team.prom
```

```sh
node_exporter --web.listen-address=127.0.0.1:9100 --collector.textfile.directory=/tmp/team-metrics
```

Have the existing Prometheus installation scrape that node_exporter. For Prometheus on the same host, merge this job into its `scrape_configs`:

```yaml
scrape_configs:
  - job_name: claude-agent-team
    static_configs:
      - targets: ['127.0.0.1:9100']
```

This is a one-shot export. Run the Python command on your own schedule (for example, every minute using cron or a systemd timer). No scheduler, listener, monitoring service or upload is installed by this project. Keep the board bound to loopback; Prometheus reads node_exporter, not the board's private snapshot endpoint. Existing collectors expose other host metrics as well; configure their access according to your monitoring setup.

Both output formats write a complete UTF-8/LF file to a temporary `.tmp` file in the destination directory, then replace the target atomically. The temporary name does not match node_exporter's `*.prom` glob. The destination directory must exist. A failed read, selector check, validation, write or replacement returns 1 and leaves existing output intact. A selector miss does not create or replace the output file. The command refuses an output that aliases the input, including hardlinks. New output files use private temporary-file permissions; on POSIX, replacing an existing file preserves its permission bits. For a separate collector service account, provision the file/directory permissions or ACLs before scheduling exports; do not assume the new file is world-readable.

Because a failed refresh leaves the last successful file in place, monitor job exit status and file freshness. The collector's `node_textfile_mtime_seconds` exposes the last successful replacement. Example PromQL for Grafana or Prometheus:

```promql
claude_agent_team_estimated_cost_usd{scope="all"}
claude_agent_team_tokens{scope="all",kind="output"}
claude_agent_team_budget_utilization_ratio{scope="session"} > 0.8
claude_agent_team_unpriced_entries > 0
time() - node_textfile_mtime_seconds{file=~".*claude_agent_team[.]prom"} > 120
absent(node_textfile_mtime_seconds{file=~".*claude_agent_team[.]prom"})
```

The last two expressions catch stale files and missing first exports, respectively. Match the filename and refresh interval to your setup; a working query is not an installed alert rule. The CLI works on Windows too, but this node_exporter recipe is for its supported host platforms. No Windows monitoring-agent integration is claimed here.

### Privacy boundary

Exported JSON and Prometheus samples contain numeric aggregates only; Prometheus labels come from fixed scope/kind enums. The optional budget gauge adds only `scope="session"`. The optional test gauge adds only `status="pass"`, `"fail"` or `"unknown"`. Session IDs, timestamps, local paths, member names, prompts, tool arguments, commands, test output, messages, model names and raw errors are excluded, including unknown nested fields added to `totals`. Aggregate activity and estimated spending may still be sensitive: inspect the metrics before sharing them. Raw snapshots and transcripts remain private and should not be attached to public issues.

## Battle card

Render one self-contained SVG from the synthetic fixture, or from a board that is already running:

```sh
python tools/battle_card.py --input examples/snapshot.synthetic.json --static --output card.svg
python tools/battle_card.py --port 7788 --theme dark --output card.svg
python tools/battle_card.py --port 7788 --session sess-0001 --static --output card.svg
python tools/battle_card.py --port 7788 --project C--demo --output card.svg
```

Choose exactly one of `--input` or `--port`. `--session` and `--project` are the same mutually exclusive selectors as [aggregate export](#export-aggregate-metrics): they require `--port`, cannot filter an offline file, and never appear on the card. The port mode performs one read-only GET to `http://127.0.0.1:<port>/snapshot.json` through `export_snapshot.read_snapshot`: proxies are bypassed, redirects are refused, the socket timeout is five seconds, and the body limit is 8 MiB. There is no arbitrary remote URL and no upload.

`--theme auto` (the default) follows `prefers-color-scheme`. `light` and `dark` bake a single palette into the presentation attributes, so the card still has color if a viewer drops the stylesheet. `--static` omits the slow dash. The file names system fonts only. It has no script, no external image and no `foreignObject`.

The card draws a section only when the snapshot already has that data:

| On the snapshot | On the card |
| --- | --- |
| `totals` with every field the aggregate export requires, plus a `members` array | Member count, estimated cost, combined tokens, cache hit, and the usage table. The figures are the `export_metrics` values. |
| `totals.unpriced` greater than zero | The cost is marked 部分. Missing prices are not drawn as zero. |
| Member `name`, `roleTitle` or `agentType`, `dept`, `status`, `cost`, `totalTokens`, `cacheHit`, `toolCount` | One row. A member with no safe name is labeled by position, such as 成员 01. |
| Both `startedAt` and `endedAt` | Elapsed time. A missing end is not replaced with the current time. |
| `departments` | A row when that department has people, an active status, or a non-zero figure. Ids inside `members` are counted, not printed. |
| `officialTeam.tasks` whose `normalizedStatus` (or `status`, when the normalized field is absent) is `completed` | Subject and owner. Task descriptions are not printed. Other statuses are not listed. |
| `stallAlerts.thresholdSec` and `stallAlerts.candidates` | How many idle, errored, or stalled candidates have `idleSeconds` at least the threshold. Names, ids, and error text are not printed. A missing or unusable block is omitted; the card does not time members itself. |
| `theaters` array | How many theater objects are present. Labels, branches, and session ids are not printed. |
| `live.available` true and integer `live.retained` | That retained hook-event count, when it is greater than zero. Event bodies are not printed. |
| `budget.enabled` true and a finite nonnegative `budget.ratio` | A budget-utilization row: the percent, a bar that stops at full width above 100%, and 预算内 / 接近预算 / 超出预算 when `status` is one of `ok`, `warn`, or `over`. A true `lowerBound` adds the fixed words 成本为下限. Names, suggestions, and `lowerBoundLabel` are not printed. |

Sections with nothing to show are omitted. The SVG does not include session ids, local paths, e-mail addresses, prompts, tool arguments, message bodies or model ids. Cost is the board's estimate, not a provider invoice. [`docs/img/battle-card.svg`](img/battle-card.svg) is this command with `--static` on `examples/snapshot.synthetic.json`.

![Battle card from the synthetic aggregate fixture](img/battle-card.svg)

The board button「导出战报卡」builds the same SVG in the browser from the snapshot the page already holds, then downloads `battle-card.svg`. Nothing is uploaded. It follows the page theme, and it omits motion when the browser requests reduced motion.

Exit code 0 means the SVG was written. Exit code 1 is a read, validation or write failure. Argparse uses 2 for invalid arguments. Diagnostics omit source values and tracebacks. The output path must not be the input file.

## File-based handoffs from other tools

The existing `team-board/msg.py` can send a handoff to a project inbox without a running board:

```sh
# Run from your project directory after creating .team/inbox/.
python /path/to/claude-agent-team/team-board/msg.py send lead "Tests passed; review the local patch." --from contributor
python /path/to/claude-agent-team/team-board/msg.py inbox lead
```

The CLI searches the current directory and parents for `.team/inbox/`. When the local board cannot be reached, `send` appends a Markdown entry; `inbox` reads the local inbox. With a running board it uses the existing message bus. This shares a handoff, not a session import, and does not cause an external coding tool to execute it automatically. Treat received content as untrusted data and review commands before running them.

To contribute a new adapter, use synthetic fixtures, preserve the read-only/privacy boundary, and describe compatibility limits in your PR. See [CONTRIBUTING.md](../CONTRIBUTING.md).
