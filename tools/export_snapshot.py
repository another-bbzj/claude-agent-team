#!/usr/bin/env python3
"""Export allowlisted aggregate metrics from a board snapshot (standard library only)."""
import argparse
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 5
USAGE_COUNTS = ('in', 'out', 'cache_read', 'cache_write', 'total')


class ExportError(ValueError):
    """A safe, actionable diagnostic containing no source snapshot values."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ExportError('board redirects are refused; check the local board port')


def _read_bounded(stream):
    data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ExportError('snapshot exceeds the 8 MiB input limit')
    return data


def _reject_constant(_value):
    raise ValueError('non-standard JSON numeric constant')


def read_snapshot(input_path=None, port=None, session=None, project=None):
    """Read an offline file or the fixed loopback endpoint, without proxy/redirects."""
    if (input_path is None) == (port is None):
        raise ExportError('choose exactly one input file or local board port')
    if session is not None and project is not None:
        raise ExportError('choose at most one session or project selector')
    selection = {}
    if session is not None:
        selection['session'] = session
    elif project is not None:
        selection['project'] = project
    if selection and input_path is not None:
        raise ExportError('session and project selectors require a local board port')
    if any(not isinstance(value, str) or not value.strip() for value in selection.values()):
        raise ExportError('session and project selectors must be non-empty strings')
    try:
        query = urllib.parse.urlencode(selection)
    except UnicodeError:
        raise ExportError('session and project selectors must be valid UTF-8') from None
    if input_path is not None:
        try:
            with Path(input_path).open('rb') as stream:
                data = _read_bounded(stream)
        except OSError:
            raise ExportError('cannot read snapshot input; check file and permissions') from None
    else:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ExportError('board port must be an integer from 1 to 65535')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        url = 'http://127.0.0.1:{}/snapshot.json'.format(port)
        if query:
            url += '?' + query
        try:
            with opener.open(url, timeout=TIMEOUT) as response:
                data = _read_bounded(response)
        except (OSError, urllib.error.URLError, HTTPException):
            raise ExportError('cannot read local board; check the port and start the board') from None
    try:
        snapshot = json.loads(data.decode('utf-8-sig'), parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ExportError('snapshot input must be valid UTF-8 JSON') from None
    if selection:
        selected = snapshot.get('session') if isinstance(snapshot, dict) else None
        field, requested = ('id', session) if session is not None else ('project', project)
        actual = selected.get(field) if isinstance(selected, dict) else None
        matches = isinstance(actual, str) and (
            actual.startswith(requested) if session is not None else requested in actual)
        if not matches:
            raise ExportError('board returned no matching session; check the selector')
    return snapshot


def _number(value, field, count=False, ratio=False):
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ExportError('{} must be a finite number'.format(field))
    if value < 0 or (count and type(value) is not int) or (ratio and value > 1):
        raise ExportError('{} has an invalid numeric range or type'.format(field))
    return value


def export_metrics(snapshot):
    """Copy explicit numeric fields only; unknown fields are intentionally ignored."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('totals'), dict):
        raise ExportError('snapshot must be an object containing a totals object')
    members = snapshot.get('members')
    if not isinstance(members, list) or any(not isinstance(member, dict) for member in members):
        raise ExportError('snapshot must contain a members array of objects')
    source = snapshot['totals']
    totals = {}
    for key in ('cost', 'membersCost', 'unpriced', 'tokens'):
        totals[key] = _number(source.get(key), 'totals.' + key, count=key in ('unpriced', 'tokens'))
    for group in ('usage', 'leadUsage', 'allUsage'):
        usage = source.get(group)
        if not isinstance(usage, dict):
            raise ExportError('totals.{} must be an object'.format(group))
        totals[group] = {key: _number(usage.get(key), 'totals.' + group + '.' + key, count=True)
                         for key in USAGE_COUNTS}
        totals[group]['cacheHit'] = _number(usage.get('cacheHit'), 'totals.' + group + '.cacheHit', ratio=True)
    return {'schemaVersion': 1, 'memberCount': len(members), 'totals': totals}


def _port(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('port must be an integer from 1 to 65535')
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('port must be an integer from 1 to 65535')
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='offline board snapshot JSON file')
    source.add_argument('--port', type=_port, help='local board port, normally 7788')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--session', help='session ID prefix (requires --port)')
    selection.add_argument('--project', help='project key or substring (requires --port)')
    parser.add_argument('--output', type=Path, help='write metrics JSON here instead of stdout')
    args = parser.parse_args(argv)
    if (args.session is not None or args.project is not None) and args.port is None:
        parser.error('--session and --project require --port')
    if any(value is not None and not value.strip() for value in (args.session, args.project)):
        parser.error('session and project selectors must be non-empty')
    try:
        if args.input and args.output:
            try:
                same_file = args.input.resolve() == args.output.resolve()
                if not same_file and args.output.exists():
                    same_file = args.input.samefile(args.output)
            except (OSError, RuntimeError):
                raise ExportError('cannot compare input and output files; check paths and permissions') from None
            if same_file:
                raise ExportError('output must differ from the snapshot input file')
        metrics = export_metrics(read_snapshot(args.input, args.port, session=args.session, project=args.project))
        result = json.dumps(metrics, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
        if args.output:
            try:
                args.output.write_text(result, encoding='utf-8')
            except OSError:
                raise ExportError('cannot write metrics output; check directory and permissions') from None
        else:
            sys.stdout.write(result)
    except ExportError as exc:
        print('export failed: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
