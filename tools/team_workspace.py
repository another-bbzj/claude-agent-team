#!/usr/bin/env python3
"""Create a file-based team workspace from a validated, offline JSON plan."""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PureWindowsPath


class PlanError(ValueError):
    """An invalid plan or unsafe destination."""


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise PlanError('{} must be a nonempty single-line string'.format(label))
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise PlanError('{} must contain valid UTF-8 text'.format(label)) from None
    return value.strip()


def _identifier(value, label):
    value = _text(value, label)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise PlanError('{} must use lowercase letters, numbers, hyphens or underscores'.format(label))
    _path(value)
    return value


def _path(value):
    value = _text(value, 'file path')
    parts = value.split('/')
    if ('\\' in value or PureWindowsPath(value).drive or value.startswith('/')
            or any(part in ('', '.', '..') for part in parts)):
        raise PlanError('file paths must be portable repository-relative paths')
    reserved = {'con', 'prn', 'aux', 'nul'} | {'{}{}'.format(p, n) for p in ('com', 'lpt') for n in range(1, 10)}
    for part in parts:
        if (part.endswith((' ', '.')) or any(c in part for c in '<>:"|?*')
                or part.split('.')[0].casefold() in reserved):
            raise PlanError('file path contains a nonportable component')
    return value


def validate_plan(plan):
    """Return normalized plan and deterministic dependency waves without writing."""
    if not isinstance(plan, dict) or set(plan) != {'name', 'tasks'}:
        raise PlanError('plan must contain exactly name and tasks')
    name = _text(plan['name'], 'name')
    if not isinstance(plan['tasks'], list) or not plan['tasks']:
        raise PlanError('tasks must be a nonempty list')
    tasks, by_id, files = [], {}, {}
    keys = {'id', 'title', 'owner', 'files', 'depends_on', 'acceptance'}
    for source in plan['tasks']:
        if not isinstance(source, dict) or set(source) != keys:
            raise PlanError('each task must contain id, title, owner, files, depends_on and acceptance')
        task = {k: _text(source[k], k) for k in ('id', 'title', 'owner')}
        task['id'] = _identifier(task['id'], 'id')
        task['owner'] = _identifier(task['owner'], 'owner')
        if task['id'] in by_id:
            raise PlanError('task ids must be unique')
        for key in ('files', 'depends_on', 'acceptance'):
            values = source[key]
            if not isinstance(values, list) or (key != 'depends_on' and not values):
                raise PlanError('{} must be {}list'.format(key, 'a ' if key == 'depends_on' else 'a nonempty '))
            task[key] = [_text(value, key) for value in values]
            if len(set(task[key])) != len(task[key]):
                raise PlanError('{} must not contain duplicates'.format(key))
        task['files'] = [_path(value) for value in task['files']]
        for path in task['files']:
            folded = path.casefold()
            if any(folded == old or folded.startswith(old + '/') or old.startswith(folded + '/') for old in files):
                raise PlanError('every file must belong to exactly one task; paths cannot overlap')
            files[folded] = task['id']
        tasks.append(task)
        by_id[task['id']] = task
    for task in tasks:
        if any(dep not in by_id for dep in task['depends_on']):
            raise PlanError('task dependency does not exist')
    waves, pending = {}, list(tasks)
    while pending:
        ready = [task for task in pending if all(dep in waves for dep in task['depends_on'])]
        if not ready:
            raise PlanError('task dependencies must form an acyclic graph')
        for task in ready:
            waves[task['id']] = 1 + max((waves[dep] for dep in task['depends_on']), default=0)
            pending.remove(task)
    return {'name': name, 'tasks': tasks}, waves


def _destination(output):
    output = Path(os.path.abspath(str(output)))
    if os.path.normcase(os.path.realpath(str(output))) != os.path.normcase(str(output)):
        raise PlanError('output must not traverse symbolic links or junctions')
    for path in (output,) + tuple(output.parents):
        junction = getattr(path, 'is_junction', lambda: False)
        if path.is_symlink() or junction():
            raise PlanError('output and its ancestors must not be symbolic links or junctions')
        if path.exists() and not path.is_dir():
            raise PlanError('output must be a directory')
    if os.path.lexists(str(output / '.team')):
        raise PlanError('.team already exists; choose another output directory')
    return output


def _write_tree(root, plan, waves):
    for folder in ('tickets', 'inbox', 'handoffs'):
        (root / folder).mkdir()
    lines = ['# ' + plan['name'], '', 'Generated from a validated plan. Fill in interface decisions before implementation.', '',
             '## Ownership and waves', '', '| Task | Owner | Wave | Files |', '| --- | --- | --- | --- |']
    for task in plan['tasks']:
        lines.append('| {} | {} | {} | {} |'.format(task['id'], task['owner'], waves[task['id']], ', '.join(task['files'])))
        content = ['# {}: {}'.format(task['id'], task['title']), '', 'owner: ' + task['owner'], 'status: todo',
                   'wave: ' + str(waves[task['id']]), 'blocked_by: ' + (', '.join(task['depends_on']) or 'none'), '',
                   '## Owned files', ''] + ['- ' + path for path in task['files']]
        content += ['', '## Acceptance', ''] + ['- [ ] ' + item for item in task['acceptance']]
        content += ['', '## Handoff', '', 'Record changed files, commands and observed results, deviations and remaining risks.', '']
        (root / 'tickets' / (task['id'] + '.md')).write_text('\n'.join(content), encoding='utf-8')
    (root / 'SPEC.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    for owner in sorted({task['owner'] for task in plan['tasks']}):
        (root / 'inbox' / (owner + '.md')).write_text('# Inbox: ' + owner + '\n', encoding='utf-8')


def _publish(stage, target):
    if os.name == 'nt':
        # Windows rename is atomic on this filesystem and refuses an existing target.
        stage.rename(target)
        return
    # POSIX rename can replace an existing empty directory. Reserve exclusively
    # instead; publication is not atomic here, but existing user files stay intact.
    target.mkdir()
    try:
        for directory in ('tickets', 'inbox', 'handoffs'):
            (target / directory).mkdir()
        for source in sorted(stage.rglob('*')):
            if source.is_file():
                destination = target / source.relative_to(stage)
                if destination.parent.is_symlink():
                    raise PlanError('workspace changed during creation')
                with destination.open('xb') as handle:
                    handle.write(source.read_bytes())
    except (OSError, PlanError):
        # Never recursively delete a published directory: another process might
        # have added user content. Leave any partial .team for human inspection.
        raise PlanError('workspace publication failed; inspect the partial .team before retrying') from None


def create_workspace(plan, output):
    """Create .team once; on POSIX a publication error leaves partial work intact.

    Windows publishes the staged directory by rename; POSIX reserves .team first
    to avoid replacing even an empty user directory. Existing work is not merged.
    """
    plan, waves = validate_plan(plan)
    output = _destination(output)
    output.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.team-stage-', dir=str(output)))
    try:
        _write_tree(stage, plan, waves)
        _destination(output)
        _publish(stage, output / '.team')
    finally:
        if stage.exists():
            shutil.rmtree(str(stage))
    return output / '.team'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'Never merges or overwrites .team. Windows publishes by atomic rename; '
        'on POSIX, publication is exclusive but not atomic, and failure retains '
        'partial .team for inspection.'))
    parser.add_argument('--plan', type=Path, required=True, help='offline JSON plan')
    parser.add_argument('--output', type=Path, required=True, help='directory in which to create .team')
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding='utf-8'))
        result = create_workspace(plan, args.output)
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        message = str(error) if isinstance(error, PlanError) else 'cannot read plan or create workspace; check JSON, paths and permissions'
        print('team_workspace: ' + message, file=sys.stderr)
        return 1
    print('Created ' + str(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
