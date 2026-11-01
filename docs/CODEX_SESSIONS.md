# Codex interactive sessions

The local board now discovers Codex rollout JSONL files under
`$CODEX_HOME/sessions` (default `~/.codex/sessions`) alongside Claude Code
subagent transcripts. This is a read-only adapter; it does not start Codex,
change its configuration, send messages into chats, or call a model API.

Start from a checkout with Python 3.8+:

```sh
python team-board/team_board.py --source all --port 7788
python team-board/team_board.py --source codex --port 7791
```

The page's source selector switches between all, Claude Code and Codex without
restarting. Each session is labelled by provider. A qualified ID such as
`--session codex:SESSION_ID` selects a specific Codex root; an unqualified ID
prefix remains supported. The newest root within the selected source is the
default. `--project` matches the discovered project label (Codex's recorded cwd).
Archived Codex sessions are not scanned automatically.

## What is shown

- The lead is labelled Codex, and its actual recorded model is displayed in full.
- Verified child sessions become members, with their own recorded model and
  activity. Parent metadata, native SubAgentActivity records and spawn results
  establish relationships; sharing a working directory does not.
- The conversation panel shows bounded visible user/assistant text, including
  linked members' text. Tool activity stays in the separate activity feed.
- Repeated cumulative usage records replace previous totals. Cached tokens are
  subsets of input; they are not counted twice. Cost uses your configured price
  table and is an estimate, not a subscription bill. Missing pricing stays
  unpriced. Switching models does not produce a historical per-model billing
  ledger: the displayed estimate uses the session's current recorded model.

System/developer messages, reasoning content and raw tool output are excluded
from conversation rows. The dashboard still contains private user messages and
local metadata: keep it on loopback and do not publish raw snapshots.

The board message composer is disabled for a Codex session; return to Codex to
continue chatting. The existing member-definition editor edits Claude Code
configuration, not running Codex models. Pet assignment changes board display
only. The official Claude Agent Teams task board does not become a Codex task
board.

## Format and limits

This adapter targets the locally observed `session_meta`, `turn_context`,
`response_item`, `event_msg` and `token_usage_record` rollout records, including
native item-completed records. Rollout storage is implementation data, not the
same event stream as `codex exec --json`. The existing offline exporter remains
separate and compatible.

Discovery selects at most 500 recent files. Parsing reads complete appended
lines incrementally, caches unchanged files and bounds large initial reads to
4 MiB plus metadata. The conversation is bounded; it is not a full chat-history
export. Malformed or oversized records, truncated history and unverified child
relationships have explicit warnings. A child whose parent is unavailable may
appear as a separate session rather than being silently hidden. Missing logs,
disabled history or an unsupported format can leave a session absent.

Synthetic regression files live in `examples/codex-sessions.synthetic/`.
No personal transcripts are distributed. Run focused checks with:

```sh
python -m unittest discover -s tests -p test_codex_sessions.py -v
python -m unittest discover -s tests -p test_codex_board.py -v
```
