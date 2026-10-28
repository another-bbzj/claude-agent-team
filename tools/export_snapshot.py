#!/usr/bin/env python3
"""Export allowlisted aggregate metrics from a board snapshot (standard library only)."""
import argparse
import json
import math
import os
import sys
import tempfile
import urllib.error
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


def read_snapshot(input_path=None, port=None):
    """Read an offline file or the fixed loopback endpoint, without proxy/redirects."""
    if (input_path is None) == (port is None):
        raise ExportError('choose exactly one input file or local board port')
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
        try:
            with opener.open('http://127.0.0.1:{}/snapshot.json'.format(port), timeout=TIMEOUT) as response:
                data = _read_bounded(response)
        except (OSError, urllib.error.URLError, HTTPException):
            raise ExportError('cannot read local board; check the port and start the board') from None
    try:
        return json.loads(data.decode('utf-8-sig'), parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ExportError('snapshot input must be valid UTF-8 JSON') from None


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


def render_prometheus(snapshot):
    """Render validated aggregates with fixed names and labels, without timestamps."""
    metrics = export_metrics(snapshot)
    totals = metrics['totals']
    lines = []

    def gauge(name, help_text, samples):
        name = 'claude_agent_team_' + name
        lines.extend(('# HELP {} {}'.format(name, help_text), '# TYPE {} gauge'.format(name)))
        for labels, value in samples:
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ExportError('Prometheus sample exceeds the finite numeric range')
            lines.append('{}{} {}'.format(name, labels, value))

    gauge('members', 'Member entries in the selected snapshot, excluding the lead.',
          [('', metrics['memberCount'])])
    gauge('estimated_cost_usd', 'Estimated snapshot cost in USD; partial when entries are unpriced.',
          [('{scope="all"}', totals['cost']), ('{scope="members"}', totals['membersCost'])])
    gauge('unpriced_entries', 'Lead or member entries without a configured model price.',
          [('', totals['unpriced'])])
    scopes = (('members', 'usage'), ('lead', 'leadUsage'), ('all', 'allUsage'))
    kinds = (('input', 'in'), ('output', 'out'), ('cache_read', 'cache_read'),
             ('cache_write', 'cache_write'), ('total', 'total'))
    gauge('tokens', 'Snapshot token usage by scope and bucket; scopes and buckets overlap.',
          [('{scope="' + scope + '",kind="' + kind + '"}', totals[group][field])
           for scope, group in scopes for kind, field in kinds])
    gauge('cache_hit_ratio', 'Board-reported snapshot cache hit ratio from zero to one.',
          [('{scope="' + scope + '"}', totals[group]['cacheHit']) for scope, group in scopes])
    return '\n'.join(lines) + '\n'


def _write_atomic(output, text):
    """Keep the previous export intact until a complete replacement is ready."""
    temporary = None
    try:
        mode = output.stat().st_mode & 0o777 if output.exists() else None
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=str(output.parent), prefix='.team-metrics-',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        if mode is not None:
            os.chmod(str(temporary), mode)
        os.replace(str(temporary), str(output))
    except OSError:
        raise ExportError('cannot write metrics output; check directory and permissions') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


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
    parser.add_argument('--format', choices=('json', 'prometheus'), default='json', help='output format (default: json)')
    parser.add_argument('--output', type=Path, help='atomically write metrics here instead of stdout')
    args = parser.parse_args(argv)
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
        snapshot = read_snapshot(args.input, args.port)
        if args.format == 'prometheus':
            result = render_prometheus(snapshot)
        else:
            result = json.dumps(export_metrics(snapshot), ensure_ascii=False, allow_nan=False, indent=2) + '\n'
        if args.output:
            _write_atomic(args.output, result)
        elif args.format == 'prometheus' and hasattr(sys.stdout, 'buffer'):
            sys.stdout.buffer.write(result.encode('utf-8'))   # Windows text stdout would translate LF to CRLF.
        else:
            sys.stdout.write(result)
    except ExportError as exc:
        print('export failed: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
