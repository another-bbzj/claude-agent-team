# Try the read-only synthetic board

Try the existing board interface without a Claude Code account, transcripts or an
installation. From a repository checkout with Python 3.8 or later:

```sh
python tools/demo_board.py --port 7790
```

Open [the local demo](http://127.0.0.1:7790/) in your browser. The command prints the
actual URL; `--port 0` chooses an available port. Stop it with Ctrl+C. It does not
open a browser automatically or install anything into your home directory.

The page displays four synthetic subagent members and two official Agent Team
teammates, plus coherent example departments, tool activity, token counts and
estimated costs. Official teammates are marked with a 官方 Team badge. When the
fixture includes `officialTeam.tasks`, the page also shows a read-only kanban with
pending, in progress and completed columns (owner, dependencies or blocked, and
time in the current state). A persistent banner marks it as a
**read-only demo with synthetic data**. All data comes from
[the synthetic fixture](../examples/board-demo.synthetic.json); displayed times
are shifted in memory so the example remains readable. The example is a fixed
scenario, not live work or evidence of project adoption. Its numbers are not a
benchmark or a real bill. The kanban in this demo does not read `~/.claude/tasks/`.

The demo serves the existing `team-board/index.html`, the fixture snapshot and
only the shipped sprite/favicon allowlist. It never imports the live backend,
reads a personal `~/.claude` or `~/.codex`, scans local transcripts, downloads
artwork, checks for updates, or connects to an external API. It binds only to
`127.0.0.1`. No authentication or API key is required.

You can inspect members and switch themes. Editing, sending messages, importing
artwork, updating and shutdown API actions are unavailable: every mutation request
returns a read-only error. Some management dialogs belong to the full board and
cannot be used in this demo. Raw API endpoints other than `/snapshot.json` are
unavailable. The demo does not use the full board's `?demo=1` overlay and redirects
that URL back to `/`, avoiding duplicate synthetic members.

To share a screenshot, keep the synthetic-data banner visible and describe it as
a demonstration. For the real transcript-based workflow, follow the
[main README](../README.md). For aggregate exports and other integrations, see
[INTEGRATIONS.md](INTEGRATIONS.md).

![Read-only synthetic demo, subagent members and official teammates](img/demo-readonly.png)

The same fixture is drawn below as a theme-aware animation. It is not a screen recording. Names, status, costs and tool labels are the fields in `examples/board-demo.synthetic.json`, formatted with the board's own cost and duration rules. The numbers are the example scenario, not a benchmark or a bill. A `Read` call can be finished while the member is still stalled: that is how this fixture is written. The animation still shows only the four synthetic subagent members. It does not draw the official Agent Team teammates or the task kanban; those appear on the live demo page, not in this SVG.

![Synthetic board drawn from examples/board-demo.synthetic.json](img/demo-synthetic.svg)
