# Ecosystem roadmap

This is a working list of extension directions, not a release schedule. Proposals should start with a concrete user problem and acceptance criteria; maintainers can revise priority based on reproducible reports and contributor capacity.

## Implemented foundation

The current board parses local Claude Code transcripts and exposes local snapshots and a message bus. The message CLI can fall back to `.team/inbox` files when the board is unreachable. Tests exercise synthetic transcripts, temporary-home installation and local HTTP behavior. Contributor instructions and review prompts are available in [AGENTS.md](../AGENTS.md) and [the Codex maintenance guide](CODEX.md).

Implemented extension tools include the [versioned aggregate snapshot export](INTEGRATIONS.md), a [Codex exec JSONL metrics adapter](CODEX_METRICS.md) with synthetic fixtures, and a [read-only no-account board demo](DEMO.md). The exec adapter is an offline event summary, with its supported format and limits documented separately. The board also shows a read-only kanban of the official Agent Teams task list for the selected session (`~/.claude/tasks/<team>/`), refreshed through the existing snapshot poll. Hovering a card highlights the member or lead with that exact name. See [AGENT_TEAMS.md](AGENT_TEAMS.md).

## Planned work

| Priority | Direction | Acceptance criteria before calling it supported |
| --- | --- | --- |
| 1 | Full interactive transcript adapters, including Codex | Extend beyond the shipped offline exec metrics adapter; use documented or observed input formats with synthetic fixtures; identify source/version; test session selection, agent identity, tool events, errors and usage deduplication; keep existing Claude Code tests passing. |
| 1 | A shared, privacy-safe fixture collection | Every fixture is synthetic or explicitly sanitized; contributors can reproduce the target bug offline; tests reject accidental inclusion of credentials, local identities and private transcript content. |
| 2 | More extension recipes | Demonstrate a specific consumer using the documented integration surface; document input/output shape and failure behavior; include an offline example and avoid requiring access to a maintainer's machine. |
| 2 | Maintainer automation using Codex | Start with drafts for triage or review; define trusted inputs, permissions, secret handling, spending limits and human approval before posting, merging or releasing; include regression checks. No such hosted automation is currently configured. |
| 3 | Compatibility and release evidence | Define a repeatable release checklist with supported interpreter/OS checks, user-configuration preservation and accurate release notes; only publish a compatibility claim after those checks pass. |
| Ongoing | Feedback from actual use | Reports include version, platform, redacted reproduction and expected outcome; changes link to an acceptance test or manual verification. Publish adoption metrics only when there is a verifiable source. |

## Propose an extension

Use the repository's feature-request form. Explain who needs the extension, one example input and expected output, privacy considerations and a small first increment. A proposal or roadmap row does not mean the feature exists. In particular, using Codex to maintain this repository does not make the board a Codex session viewer.

Implemented integration commands are documented separately in [INTEGRATIONS.md](INTEGRATIONS.md). Contribution and security reporting paths are in [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md).
