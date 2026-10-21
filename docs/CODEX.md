# Maintaining this project with Codex

Codex is a contributor tool for reproducing bugs, preparing fixes, reviewing changes and triaging reports in this repository. The board itself reads Claude Code transcripts from `~/.claude/projects/`. The separate [Codex exec metrics adapter](CODEX_METRICS.md) summarizes explicitly supplied JSONL logs offline. It does not import interactive Codex sessions into the board or invoke an OpenAI API.

## Start with a bounded task

Open a checkout in your Codex client and ask it to read the repository's [AGENTS.md](../AGENTS.md) and [contribution guide](../CONTRIBUTING.md). The repository instructions give the source map, test commands and privacy constraints. OpenAI documents how Codex discovers these files in [Custom instructions with AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

A useful bug-fix prompt is:

```text
Investigate this redacted reproduction: <steps and expected/actual behavior>.
Read AGENTS.md and the relevant implementation and tests. Reproduce the failure
with synthetic fixtures in a temporary home. Add a regression test that fails
before the fix, then make the smallest correction. Preserve Python 3.8 support
and existing user configuration. Report the test commands, results and remaining
uncertainty. Do not install into my real home, commit, push or merge.
```

Replace the angle-bracket text with actual evidence. Remove private messages, credentials and identifying paths before including a report.

## Reproduce and verify

The project uses `unittest` from the Python standard library. Run from the repository root:

```sh
python -m unittest discover -s tests -v
```

For installer work, ask Codex to follow the temporary-home pattern in `tests/test_install.py`. For parser or HTTP work, follow the synthetic `BoardEnv` fixture in `tests/test_board.py`. Tests that start local HTTP servers need loopback socket access; a sandbox denial is not a successful test or a reproduced product defect.

For UI changes, run the JavaScript syntax command in [AGENTS.md](../AGENTS.md) and verify the changed interaction in a browser. A syntax pass alone cannot establish layout or interaction correctness.

## Review independently

OpenAI's [Code review documentation](https://learn.chatgpt.com/docs/code-review) describes reviewing local changes and pull requests. For a local checkout, this prompt also makes the requested review scope explicit:

```text
Review the diff between <base commit> and <head commit>. Do not edit files.
Focus on data loss, request validation, startup/update failure paths, Python 3.8
compatibility and missing regression coverage. For each actionable finding,
include the triggering input, observed behavior, expected behavior and file/line.
Distinguish verified defects from questions. If no actionable issue is found,
say so and list what was not verified. Do not approve or merge the PR.
```

Use real commit IDs. Inspect each finding against the current diff and tests. A maintainer remains responsible for checking CI and deciding whether to publish feedback or merge; a model's review is supporting evidence.

## Triage without changing public state

```text
Read this redacted bug report and the relevant source. Draft a triage reply
containing a concise reproduction summary, likely affected area, missing
information and a proposed acceptance test. Do not assign labels, close issues
or post a reply. Do not infer adoption or severity without evidence.
```

For a release check, ask for a comparison of `VERSION`, the diff since the intended base and CI results. Have Codex draft release notes only for verified changes; publishing is a separate maintainer action.

## Configuration and boundaries

No OpenAI API key is required to run the board or its test suite. Codex access and authentication are configured in the contributor's own Codex client. This repository does not configure hosted Codex reviews, an OpenAI API integration or a workflow that automatically spends API credits.

A future automation proposal should name its data inputs, secret handling, approval boundaries, cost limits and regression checks before it is enabled. See the [roadmap](ROADMAP.md) for planned extension work and the [integration guide](INTEGRATIONS.md) for implemented extension surfaces. The project is independent and does not imply OpenAI endorsement or participation in an OpenAI program.
