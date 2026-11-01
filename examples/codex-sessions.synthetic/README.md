# Synthetic Codex interactive sessions

These invented rollouts exercise visible conversation, current model, repeated
cumulative usage and verified parent/child relations. They contain no personal
transcripts. `root.jsonl` is the lead, `child.jsonl` has explicit parent metadata,
`other-child.jsonl` is correlated by a parent activity event, and `unrelated.jsonl`
has the same relative working directory but no parent relation.

Tests copy these into a temporary session directory; no Codex or model calls run.
