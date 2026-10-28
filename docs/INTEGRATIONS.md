# Integration recipes

These recipes use the existing Claude Code board and standard-library tools. They do not add a hosted service or interactive Codex session ingestion. See [the main guide](GUIDE.md) for board setup and [the Codex maintainer guide](CODEX.md) for using Codex to maintain this repository.

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

Python 3.8+ and its standard library are sufficient. Choose exactly one of `--input` or `--port`. The output directory must already exist. The exporter refuses an output path that resolves to the input file, including an existing hardlink alias. Exit code 0 means success; 1 means a read, validation or write failure; argparse uses 2 for invalid command-line arguments. Error messages omit source data and tracebacks.

The live mode performs one read-only GET to `http://127.0.0.1:<port>/snapshot.json`, bypasses environment/system HTTP proxies, refuses all redirects, and has a five-second socket timeout. Both file and HTTP inputs have an 8 MiB limit. There is no arbitrary remote-URL option or automatic upload.

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

### Privacy boundary

Exported JSON contains numeric aggregates only. Session IDs, timestamps, local paths, member names, prompts, tool arguments, messages, model names and raw errors are excluded, including unknown nested fields added to `totals`. Aggregate activity and estimated spending may still be sensitive: inspect the metrics before sharing them. Raw snapshots and transcripts remain private and should not be attached to public issues.

## Battle card

Render one self-contained SVG from the synthetic fixture, or from a board that is already running:

```sh
python tools/battle_card.py --input examples/snapshot.synthetic.json --static --output card.svg
python tools/battle_card.py --port 7788 --theme dark --output card.svg
```

Choose exactly one of `--input` or `--port`. The port mode performs one read-only GET to `http://127.0.0.1:<port>/snapshot.json` through `export_snapshot.read_snapshot`: proxies are bypassed, redirects are refused, the socket timeout is five seconds, and the body limit is 8 MiB. There is no arbitrary remote URL and no upload.

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
