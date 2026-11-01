# Offline team workspace

The free `tools/team_workspace.py` helper turns an explicit JSON plan into a
project-local `.team/` directory. It starts no board, makes no network calls,
does not invoke models, and changes no home configuration. Run it from a
repository checkout with Python 3.8+; it is not installed as a global command.

```json
{
  "name": "CSV report",
  "tasks": [
    {
      "id": "01-report",
      "title": "Implement validated report",
      "owner": "implementer",
      "files": ["summarize.py"],
      "depends_on": [],
      "acceptance": ["Aggregate quantity times unit price and sort item names"]
    },
    {
      "id": "02-review",
      "title": "Verify regression evidence",
      "owner": "reviewer",
      "files": ["review.md"],
      "depends_on": ["01-report"],
      "acceptance": ["Record the tests run and unresolved findings"]
    }
  ]
}
```

Save that as `plan.json`, then choose the project directory:

```sh
python tools/team_workspace.py --plan plan.json --output path/to/project
```

The result contains `SPEC.md`, one ticket per task, one inbox per owner, and an
empty handoffs directory. Dependency depth determines each ticket's wave.
Files have one owner; missing dependencies, cycles, overlapping ownership and
nonportable paths are rejected before writing. Owners are role labels, not
assertions that an agent with that name exists.

Fill in interface decisions in SPEC before implementing. Native parallel
delegation is optional: without it, perform the roles sequentially and record
that honestly. The dashboard is optional for this file-based workflow.

The command refuses an existing `.team`, including an empty directory or a
symlink; it does not merge, reset or overwrite your work. Exit 0 means created,
1 means invalid input or an I/O failure, and 2 means invalid command arguments.
Windows uses staged directory rename on the same filesystem. On POSIX the
destination is reserved exclusively and then filled; readers may see a partial
workspace during creation. If publication fails there, inspect the partial
`.team` before choosing a new output directory. Concurrent hostile changes to
the destination are outside this local authoring helper's isolation guarantees.

This helper is part of the free MIT project. It requires no paid companion,
marketplace account or external workflow package.
