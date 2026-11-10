# Synthetic Codex rollout variants

Invented JSONL for format differences the board maps onto the same session
view as Claude Code: members, plan tasks, tool calls, and token totals.
`rollout.jsonl` is the lead. `child.jsonl` is a sub-agent whose `session_id`
is the lead's id and whose own `id` is different. A resumed session is not a
second file here; tests cover that with a temporary copy.

Blank lines, a non-object line and a broken JSON line sit between real
records. Tool output, patch text, MCP arguments and ciphertext stay in this
file only so tests can prove the parser does not copy them. There are no home
directories, account ids or credentials. Working directories are the relative
label `demo-project`.
