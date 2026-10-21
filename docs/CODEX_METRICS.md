# Codex exec event metrics

`tools/codex_metrics.py` is an offline adapter for a capture of the documented
`codex exec --json` stdout stream. It uses Python 3.8+ standard-library modules;
running the adapter or its synthetic example needs no account, API key or network.
It does not invoke Codex, scan `~/.codex`, parse internal rollout files or add
Codex sessions to the live Claude Code board.

## Try the synthetic example

From the repository root:

```sh
python tools/codex_metrics.py --input examples/codex-exec.synthetic.jsonl
python tools/codex_metrics.py --input examples/codex-exec.synthetic.jsonl --output metrics.json
```

The example contains two independent completed turns with equal usage. Its output
includes 200 input tokens, 120 cached input tokens and 50 output tokens: 250 total,
not 370. It also includes two completed command items and one agent message.

For a real, separately authorized Codex run, save its JSONL stdout locally, then
pass that file explicitly to this adapter. Raw events may contain commands, file
paths, messages and tool output; keep the capture private. See the official
[non-interactive mode documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
for running and authenticating Codex itself. No model or CLI version is pinned by
this adapter; compatibility is with the documented event fields and synthetic
fixture, checked on 2026-10-21. The wire names `todo_list` (plan updates) and
item-level `error` are also checked against OpenAI's
[SDK item definitions](https://github.com/openai/codex/blob/main/sdk/typescript/src/items.ts).

## Output format, version 1

Output has `schemaVersion: 1`, `source: "codex-exec-jsonl"`, and three objects whose
values are nonnegative integer aggregates:

| Object | Fields and interpretation |
| --- | --- |
| `counts` | Threads; turns started, completed, failed and incomplete; top-level error events; item completions; tool completions and failed tool completions; unknown event/item counts. |
| `itemTypes` | Fixed keys: `agent_message`, `reasoning`, `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, `todo_list`, `error`. Unknown item types cannot introduce new output keys. |
| `usage` | `inputTokens`, `cachedInputTokens`, `outputTokens`, `totalTokens`. Optional `reasoningOutputTokens` and `reasoningMeasuredTurns` indicate the sum and coverage of turns that explicitly report reasoning usage. |

`cachedInputTokens` is a subset of input tokens. `totalTokens` is input plus output;
neither cached nor reasoning tokens are added again. Reasoning coverage can be
partial when some turns omit the field; absence does not mean zero reasoning.
Only completed turns contribute usage. Failed or interrupted turns may have spent
tokens that the event stream does not report, so this is not a complete billing
record. No costs or provider prices are estimated.

Tool completions are command execution, file change, MCP tool call and web search
items. A tool failure is counted when its completed item explicitly has
`status: "failed"`; other status values are not interpreted as failures. Item
started/updated events do not inflate completion counts.

## Lifecycle and compatibility limits

- Each file represents at most one thread. A second `thread.started` is rejected;
  do not concatenate captures. A standalone fatal `error` event is accepted and
  counted, even when no thread was created.
- Turns need an observed `turn.started` before their terminal event. A second
  terminal event without the next start is rejected rather than double counted.
  Equal usage in different turns is counted independently.
- Item completion replays with the same ID are counted once within a turn. IDs
  may be reused in later turns; conflicting completed item types are rejected.
  There is no cross-file deduplication or claim that replayed captures are new work.
- A stream ending during a turn reports `turnsIncomplete: 1`. Failed turns and
  error events remain separate counts; an error can be recoverable.
- Unknown event types are ignored except for an aggregate counter. Unknown-only
  input, internal rollout-only files, invalid JSON and invalid known fields fail.
  Item events require an active turn. Partial captures missing their initial
  lifecycle events are unsupported.

## Data and failure boundaries

No thread/item IDs, model names, prompts, messages, commands, tool output, local
paths or error details are copied to the report. Numeric activity counts can still
be sensitive; inspect them before sharing. Unknown source keys never become output
keys. This format is separate from the board aggregate export described in
[INTEGRATIONS.md](INTEGRATIONS.md).

Input is bounded to 8 MiB total and 1 MiB per line, including line endings. Errors
contain safe line numbers and fixed diagnostics, not captured text or tracebacks.
Exit codes are 0 for a valid report, 1 for read/validation/write failure, and 2 for
invalid command-line arguments. An input file cannot also be the output file,
including existing hardlink aliases. File output uses a temporary file in the
destination directory and atomic replacement; failed parsing or replacement
preserves existing output. The destination directory must already exist.

Tests use only synthetic events and temporary files:

```sh
python -m unittest discover -s tests -p test_codex_metrics.py -v
```
