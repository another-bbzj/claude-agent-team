#!/usr/bin/env python3
"""Read an explicit codex exec --json capture and export private-data-free metrics."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
ITEM_TYPES = ('agent_message', 'reasoning', 'command_execution', 'file_change',
              'mcp_tool_call', 'web_search', 'todo_list', 'error')
TOOL_TYPES = ('command_execution', 'file_change', 'mcp_tool_call', 'web_search')


class MetricsError(ValueError):
    """A diagnostic containing no captured event values."""


def _reject_constant(_value):
    raise ValueError('non-standard JSON constant')


def _count(value, field, line):
    if type(value) is not int or value < 0:
        raise MetricsError('line {}: {} must be a nonnegative integer'.format(line, field))
    return value


def summarize(stream):
    """Consume one thread's complete or interrupted binary JSONL capture."""
    counts = dict.fromkeys(('threads', 'turnsStarted', 'turnsCompleted', 'turnsFailed',
                           'turnsIncomplete', 'errors', 'itemsCompleted',
                           'toolCallsCompleted', 'toolCallsFailed', 'unknownEvents',
                           'unknownItems'), 0)
    kinds = dict.fromkeys(ITEM_TYPES, 0)
    usage = dict.fromkeys(('inputTokens', 'cachedInputTokens', 'outputTokens', 'totalTokens'), 0)
    total = recognized = line_number = 0
    active = False
    completed_items = {}
    while True:
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        line_number += 1
        total += len(raw)
        if len(raw) > MAX_LINE_BYTES or total > MAX_BYTES:
            raise MetricsError('line {}: input exceeds capture or line size limit'.format(line_number))
        try:
            text = raw.decode('utf-8-sig' if line_number == 1 else 'utf-8')
            if not text.strip():
                continue
            event = json.loads(text, parse_constant=_reject_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise MetricsError('line {}: expected valid UTF-8 JSON'.format(line_number)) from None
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            raise MetricsError('line {}: expected an event object with a type'.format(line_number))
        kind = event['type']
        if kind == 'thread.started':
            if counts['threads'] or not isinstance(event.get('thread_id'), str) or not event['thread_id']:
                raise MetricsError('line {}: expected one thread.started event per capture'.format(line_number))
            counts['threads'] = 1
        elif kind == 'turn.started':
            if not counts['threads'] or active:
                raise MetricsError('line {}: invalid turn start sequence'.format(line_number))
            active = True
            completed_items = {}
            counts['turnsStarted'] += 1
        elif kind in ('turn.completed', 'turn.failed'):
            if not active:
                raise MetricsError('line {}: terminal turn event needs a preceding turn.started'.format(line_number))
            if kind == 'turn.completed':
                values = event.get('usage')
                if not isinstance(values, dict):
                    raise MetricsError('line {}: completed turn needs a usage object'.format(line_number))
                inputs = _count(values.get('input_tokens'), 'input_tokens', line_number)
                cached = _count(values.get('cached_input_tokens'), 'cached_input_tokens', line_number)
                outputs = _count(values.get('output_tokens'), 'output_tokens', line_number)
                if cached > inputs:
                    raise MetricsError('line {}: cached input exceeds total input'.format(line_number))
                usage['inputTokens'] += inputs
                usage['cachedInputTokens'] += cached
                usage['outputTokens'] += outputs
                usage['totalTokens'] += inputs + outputs
                if 'reasoning_output_tokens' in values:
                    reasoning = _count(values['reasoning_output_tokens'], 'reasoning_output_tokens', line_number)
                    usage['reasoningOutputTokens'] = usage.get('reasoningOutputTokens', 0) + reasoning
                    usage['reasoningMeasuredTurns'] = usage.get('reasoningMeasuredTurns', 0) + 1
                counts['turnsCompleted'] += 1
            else:
                counts['turnsFailed'] += 1
            active = False
        elif kind in ('item.started', 'item.updated', 'item.completed'):
            if not active:
                raise MetricsError('line {}: item event needs an active turn'.format(line_number))
            item = event.get('item')
            if not isinstance(item, dict) or not isinstance(item.get('type'), str) or not isinstance(item.get('id'), str) or not item['id']:
                raise MetricsError('line {}: item needs an id and type'.format(line_number))
            if kind == 'item.completed':
                item_id, item_type = item['id'], item['type']
                if item_id in completed_items:
                    if completed_items[item_id] != item_type:
                        raise MetricsError('line {}: completed item id changed type'.format(line_number))
                    continue
                completed_items[item_id] = item_type
                counts['itemsCompleted'] += 1
                if item_type in kinds:
                    kinds[item_type] += 1
                else:
                    counts['unknownItems'] += 1
                if item_type in TOOL_TYPES:
                    counts['toolCallsCompleted'] += 1
                    if item.get('status') == 'failed':
                        counts['toolCallsFailed'] += 1
        elif kind == 'error':
            counts['errors'] += 1
        else:
            counts['unknownEvents'] += 1
            continue
        recognized += 1
    if not recognized:
        raise MetricsError('no supported codex exec events found; provide one exec --json capture')
    counts['turnsIncomplete'] = int(active)
    return {'schemaVersion': 1, 'source': 'codex-exec-jsonl', 'counts': counts,
            'itemTypes': kinds, 'usage': usage}


def _guard_output(source, output):
    try:
        if source.resolve() == output.resolve() or (output.exists() and source.samefile(output)):
            raise MetricsError('output must differ from the input capture')
    except (OSError, RuntimeError):
        raise MetricsError('cannot compare input and output files; check permissions') from None


def _write_atomic(output, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=str(output.parent), prefix='.codex-metrics-',
                                         suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(str(temporary), str(output))
    except OSError:
        raise MetricsError('cannot write output; check directory and permissions') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True, help='one offline exec --json capture')
    parser.add_argument('--output', type=Path, help='write metrics JSON atomically instead of stdout')
    args = parser.parse_args(argv)
    try:
        if args.output is not None:
            _guard_output(args.input, args.output)
        try:
            with args.input.open('rb') as stream:
                report = summarize(stream)
        except OSError:
            raise MetricsError('cannot read input capture; check file and permissions') from None
        result = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + '\n'
        if args.output is not None:
            _write_atomic(args.output, result)
        else:
            sys.stdout.write(result)
    except MetricsError as exc:
        print('metrics failed: {}'.format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
