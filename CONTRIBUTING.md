# Contributing

Thanks for helping maintain claude-agent-team. Bug reports, reproducible test cases,
documentation fixes and small integrations are welcome. English and Chinese are both
welcome; the main [README](README.md) and [illustrated guide](docs/GUIDE.md) are in Chinese.
The English landing page is [README.en.md](README.en.md); its operational guide is [docs/GUIDE.en.md](docs/GUIDE.en.md).

## Start here

1. Check existing [issues](https://github.com/another-bbzj/claude-agent-team/issues)
   and [pull requests](https://github.com/another-bbzj/claude-agent-team/pulls).
2. For a bug, describe the expected and actual result, OS, Python version, project
   version and the smallest **redacted** reproduction. Use the bug report form.
3. For a larger feature or compatibility change, open a feature request with the
   user need and acceptance criteria before implementing it.
4. Fork the repository and work on a branch. Keep each PR focused on one problem.

Do not attach real conversations, raw session snapshots, credentials, private local
paths or downloaded artwork. Prefer synthetic fixtures. Report vulnerabilities
using [SECURITY.md](SECURITY.md), rather than a public bug report.

## Development and checks

The board and installer use Python 3.8+ standard-library modules. From a repository
checkout, run the existing tests without installing into your personal home:

```sh
python -m unittest discover -s tests -v
```

The integration tests start temporary local HTTP servers. Installer tests use
temporary `HOME` and `USERPROFILE` directories and disable automatic restart with
`TEAM_BOARD_NO_RESTART=1`; follow that pattern in `tests/test_install.py` when adding
installer tests. Never point a test at your real `~/.claude` configuration.
Some image-fitting coverage is skipped when optional Pillow is unavailable.

If a restricted environment blocks its default temporary directory, select a
writable temporary directory inside your workspace before running tests. A sandbox
may also need permission to connect to its own local test servers.

For changes to the inline JavaScript, run the same syntax check as CI (Node.js is
needed for this check, not for running the board):

```sh
node -e "const s=require('fs').readFileSync('team-board/index.html','utf8'); new Function(s.slice(s.indexOf('<script>')+8, s.lastIndexOf('</script>'))); console.log('index.html js ok')"
```

[CI](.github/workflows/test.yml) runs the test suite and JavaScript syntax check on
Windows, Linux and macOS, with Python versions defined in its matrix. For UI
changes, also check the actual interaction in light and dark themes and a narrow
viewport; a syntax check does not validate visual behavior.

## Change and review conventions

- First reproduce a bug, then add a regression test that fails before the fix.
  Use temporary files and synthetic transcripts; do not require network downloads
  or paid API calls for regression coverage.
- Preserve Python 3.8 compatibility and the zero-dependency runtime. Discuss a new
  runtime dependency before adding it.
- Preserve user edits, custom agents, settings and imported sprites when changing
  install/update behavior. Treat `team-board/bus/`, `backdrops/` and `server.log`
  as local data, not distributable fixtures.
- Describe the concrete before/after behavior and the commands you ran in the PR.
  Say which checks you could not run and why. AI-assisted changes follow the same
  review and testing requirements.
- Keep unrelated formatting, generated assets and local coordination files out of
  the PR. Do not include third-party artwork without redistribution rights.

The backend is `team-board/team_board.py`, the frontend is
`team-board/index.html`, installation is `install.py`, and regression coverage is
in `tests/`. The board reads local Claude Code transcripts and serves a local
dashboard; keep that local-data boundary explicit in any new integration.
