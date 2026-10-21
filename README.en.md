# claude-agent-team

A department-based agent team workflow and local live board for Claude Code: see which agents are working, what tools they use, how they communicate and their estimated token costs.

[中文](README.md) · [Illustrated guide (Chinese)](docs/GUIDE.md) · [Contribute](CONTRIBUTING.md) · [Security](SECURITY.md)

[![test](https://github.com/another-bbzj/claude-agent-team/actions/workflows/test.yml/badge.svg)](https://github.com/another-bbzj/claude-agent-team/actions/workflows/test.yml)

## Quick start

To try the board without installation, accounts or access to your own transcripts, run `python tools/demo_board.py --port 7790` from the checkout and open [the read-only demo](http://127.0.0.1:7790/). It uses synthetic data only; see [the demo guide](docs/DEMO.md).

Requirements: Python 3.8 or later and Claude Code for the transcript-based workflow. The core runtime uses the Python standard library. Git is needed for the clone commands below.

```sh
git clone https://github.com/another-bbzj/claude-agent-team.git
cd claude-agent-team
python install.py
```

The installer writes into your `~/.claude` directory: agent definitions, the team skill, board files, a marked block in `CLAUDE.md`, and a `SessionStart` hook in `settings.json`. It backs up existing settings before merging. Installation is intended for your own configured environment; development tests use temporary homes instead.

Start a new Claude Code session and ask it to use `agent team` for a task. Open [the local board](http://127.0.0.1:7788/). To inspect the source checkout's board directly, run:

```sh
python team-board/team_board.py
```

This starts the foreground server on the loopback address at port 7788; use Ctrl+C to stop it. It reads the current user's Claude Code project transcripts. Without transcripts, there is no agent session to display.

## What it does

- Provides eight agent definitions organized into departments and a team skill with tickets, inboxes and handoffs.
- Reads Claude Code transcript files to derive agent status, tool activity, communication and usage estimates.
- Shows a local browser board with departments, agent details, activity and a message channel.
- Offers `team-board/msg.py` for messaging, with an inbox-file fallback when the board is unreachable.
- Supports local pet artwork imports and optional background customization. The [Chinese guide](docs/GUIDE.md) covers the full UI.

The board currently parses Claude Code sessions. The [Codex exec metrics adapter](docs/CODEX_METRICS.md) separately summarizes explicitly provided `codex exec --json` logs offline. Full interactive Codex session ingestion into the board remains [planned](docs/ROADMAP.md). Using Codex to maintain the repository and importing local Codex pet artwork are separate features.

## How the pieces connect

The diagram follows the system light or dark theme. Solid lines are the live path: the lead writes `.team/` in the project, the eight member files live in `agents/`, and `team_board.py` reads transcripts only on `127.0.0.1:7788` and serves `/snapshot.json`. Dashed lines are three tools that do not read personal sessions.

![Loopback runtime: main session, resident members, the board, and three tools that do not read sessions](docs/img/architecture.svg)

## Configuration and privacy

Defaults for departments, agent labels, pets and estimated pricing live in `team-board/team.json`; the installed copy is under `~/.claude/team-board/`. Pricing is an estimate, not a provider bill. See the [Chinese README](README.md) for customization and update behavior.

The HTTP service binds to `127.0.0.1`. Local transcripts, bus messages, imported artwork and background files can contain private or licensed material. Keep them out of issues and commits; use synthetic fixtures and redact screenshots. Optional update checks, downloads and artwork imports have their own inputs, so review those actions before using them. The board does not require an OpenAI API key.

## Integrate and contribute

See [INTEGRATIONS.md](docs/INTEGRATIONS.md) for implemented extension commands and examples, [CONTRIBUTING.md](CONTRIBUTING.md) for focused bug fixes and new features, and [SECURITY.md](SECURITY.md) for security reports. The [roadmap](docs/ROADMAP.md) separates future adapters and automation from current behavior.

## Source map

| Path | Purpose |
| --- | --- |
| `team-board/` | Python server, embedded-JavaScript UI, configuration, messaging and local asset tools |
| `agents/` | Claude Code agent definitions |
| `skills/agent-team/` | Team workflow and office conventions |
| `install.py` | Install, merge configuration and uninstall |
| `tests/` | Standard-library tests and synthetic fixtures |
| `tools/` | Repository maintenance and integration utilities |
| `docs/` | Usage, integration and contributor guides |

## Development and tests

Run from the repository root:

```sh
python -m unittest discover -s tests -v
```

Some tests start local HTTP servers and need loopback socket access. The optional Pillow-dependent image test is skipped when Pillow is unavailable. UI syntax checking uses Node.js during development; use the exact command in [AGENTS.md](AGENTS.md). The [CI workflow](.github/workflows/test.yml) defines the platform/interpreter matrix.

The architecture is a Python standard-library HTTP server that derives snapshots from local transcript files, plus a browser UI that polls those snapshots. The installer and local asset/update helpers are separate scripts. No hosted backend is required to view local sessions. The figure in [How the pieces connect](#how-the-pieces-connect) is that path.

Licensed under [MIT](LICENSE). This is an independent community project.
