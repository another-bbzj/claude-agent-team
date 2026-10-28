#!/usr/bin/env python3
"""把 Claude Code 钩子 stdin 收成一条短事件，POST 到本机看板。

看板不在线、JSON 不完整、或网络失败时都静默退出，退出码 0。
不往 stdout / stderr 写任何内容：PreToolUse / Stop 会把 stdout 当成决策。

字段以 Claude Code hooks 参考为准（https://code.claude.com/docs/en/hooks）：
只留会话 id、事件名、工具名、tool_use_id、成员 / subagent 标识、通知类型、
stop_hook_active 和时间。不转发 tool_input、tool_response、提示词或本机路径。
和转录去重是后续步骤；这里的 tool_use_id 只是留给那一步的关联键。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

# 安装器挂上的事件。别的事件即使被指到这个脚本，也不入库。
EVENTS = ('PreToolUse', 'PostToolUse', 'SubagentStop', 'Notification', 'Stop')
STRING_KEYS = ('session_id', 'hook_event_name', 'tool_name', 'tool_use_id', 'agent_id', 'agent_type', 'notification_type')
BOOL_KEYS = ('stop_hook_active',)
FIELD_LIMIT = {
    'session_id': 128,
    'event': 64,
    'tool_name': 128,
    'tool_use_id': 128,
    'agent_id': 128,
    'agent_type': 128,
    'notification_type': 64,
}
PATH = '/api/hooks/event'
BODY_LIMIT = 4096
SOCKET_TIMEOUT = 0.2
STDIN_SCAN_LIMIT = 256 * 1024
SOURCE = 'claude-hook'
# 本机看板：绕过系统 / 环境 HTTP 代理。和 ensure.py 同一做法。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def clip_text(value, limit):
    """标识符。过长、换行或非字符串一律丢掉，避免把正文切一段留下来。"""
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or len(value) > limit:
        return ''
    if any(ch in value for ch in ('\n', '\r', '\x00', '/', '\\')):
        return ''
    return value


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    if value != value:
        return False
    return value not in (float('inf'), float('-inf'))


def event_ts(value, now):
    if not _finite_number(value):
        return float(now)
    if value < now - 7 * 86400 or value > now + 60:
        return float(now)
    return float(value)


def normalize_client_event(payload, now=None):
    """看板接收端用的白名单。未知字段（含工具输入输出）直接丢掉。"""
    if not isinstance(payload, dict):
        raise ValueError('请求体必须是 JSON 对象')
    source = payload.get('source', SOURCE)
    if source != SOURCE:
        raise ValueError('不接受的来源')
    now = time.time() if now is None else now
    event_name = clip_text(payload.get('event'), FIELD_LIMIT['event'])
    if event_name not in EVENTS:
        raise ValueError('不接受的事件')
    session_id = clip_text(payload.get('session_id'), FIELD_LIMIT['session_id'])
    if not session_id:
        raise ValueError('缺少会话 id')
    out = {
        'v': 1,
        'source': SOURCE,
        'session_id': session_id,
        'event': event_name,
        'ts': event_ts(payload.get('ts'), now),
    }
    for key in ('tool_name', 'tool_use_id', 'agent_id', 'agent_type'):
        text = clip_text(payload.get(key), FIELD_LIMIT[key])
        if text:
            out[key] = text
    if event_name == 'Notification':
        kind = clip_text(payload.get('notification_type'), FIELD_LIMIT['notification_type'])
        if kind:
            out['notification_type'] = kind
    if event_name in ('Stop', 'SubagentStop') and type(payload.get('stop_hook_active')) is bool:
        out['stop_hook_active'] = payload['stop_hook_active']
    return out


class _Scan(object):
    def __init__(self, text):
        self.text = text
        self.n = len(text)
        self.i = 0

    def skip_ws(self):
        text, n = self.text, self.n
        i = self.i
        while i < n and text[i] in ' \t\r\n':
            i += 1
        self.i = i

    def parse_string(self):
        text, n = self.text, self.n
        i = self.i
        if i >= n or text[i] != '"':
            return None
        i += 1
        out = []
        while i < n:
            ch = text[i]
            i += 1
            if ch == '"':
                self.i = i
                return ''.join(out)
            if ch == '\\':
                if i >= n:
                    return None
                esc = text[i]
                i += 1
                mapped = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
                if esc in mapped:
                    out.append(mapped[esc])
                elif esc == 'u' and i + 4 <= n:
                    hexpart = text[i:i + 4]
                    i += 4
                    try:
                        out.append(chr(int(hexpart, 16)))
                    except ValueError:
                        return None
                else:
                    return None
            else:
                out.append(ch)
        return None

    def skip_value(self):
        self.skip_ws()
        if self.i >= self.n:
            return False
        ch = self.text[self.i]
        if ch == '"':
            return self.parse_string() is not None
        if ch == '{':
            return self.skip_object()
        if ch == '[':
            return self.skip_array()
        if ch == '-' or ch.isdigit():
            i = self.i + 1
            while i < self.n and self.text[i] in '0123456789+-eE.':
                i += 1
            self.i = i
            return True
        for lit in ('true', 'false', 'null'):
            if self.text.startswith(lit, self.i):
                self.i += len(lit)
                return True
        return False

    def skip_object(self):
        return self._skip_container('{', '}', pairs=True)

    def skip_array(self):
        return self._skip_container('[', ']', pairs=False)

    def _skip_container(self, open_ch, close_ch, pairs):
        if self.i >= self.n or self.text[self.i] != open_ch:
            return False
        self.i += 1
        while True:
            self.skip_ws()
            if self.i < self.n and self.text[self.i] == close_ch:
                self.i += 1
                return True
            if pairs:
                if self.parse_string() is None:
                    return False
                self.skip_ws()
                if self.i >= self.n or self.text[self.i] != ':':
                    return False
                self.i += 1
            if not self.skip_value():
                return False
            self.skip_ws()
            if self.i < self.n and self.text[self.i] == ',':
                self.i += 1
                continue
            if self.i < self.n and self.text[self.i] == close_ch:
                self.i += 1
                return True
            return False


def parse_hook_text(text):
    """从钩子 JSON 里抽出顶层白名单字段。嵌套字符串里的同名键不算。"""
    if text.startswith('\ufeff'):
        text = text[1:]
    scan = _Scan(text)
    scan.skip_ws()
    if scan.i >= scan.n or scan.text[scan.i] != '{':
        return None
    scan.i += 1
    found = {}
    while True:
        scan.skip_ws()
        if scan.i < scan.n and scan.text[scan.i] == '}':
            break
        if scan.i >= scan.n:
            break
        key = scan.parse_string()
        if key is None:
            break
        scan.skip_ws()
        if scan.i >= scan.n or scan.text[scan.i] != ':':
            break
        scan.i += 1
        scan.skip_ws()
        if key in STRING_KEYS and scan.i < scan.n and scan.text[scan.i] == '"':
            value = scan.parse_string()
            if value is None:
                break
            found[key] = value
        elif key in BOOL_KEYS and (scan.text.startswith('true', scan.i) or scan.text.startswith('false', scan.i)):
            found[key] = scan.text.startswith('true', scan.i)
            scan.i += 4 if found[key] else 5
        elif not scan.skip_value():
            break
        scan.skip_ws()
        if scan.i < scan.n and scan.text[scan.i] == ',':
            scan.i += 1
            continue
        break
    if not found:
        return None
    payload = {
        'source': SOURCE,
        'event': found.get('hook_event_name') or '',
        'session_id': found.get('session_id') or '',
        'ts': time.time(),
    }
    for key in ('tool_name', 'tool_use_id', 'agent_id', 'agent_type', 'notification_type'):
        if key in found:
            payload[key] = found[key]
    if 'stop_hook_active' in found:
        payload['stop_hook_active'] = found['stop_hook_active']
    try:
        return normalize_client_event(payload, payload['ts'])
    except ValueError:
        return None


def slim_bytes(raw):
    if not raw:
        return None
    if not isinstance(raw, (bytes, bytearray)):
        return None
    text = bytes(raw[:STDIN_SCAN_LIMIT]).decode('utf-8', 'replace')
    return parse_hook_text(text)


def board_port(default=7788):
    raw = os.environ.get('TEAM_BOARD_PORT', '')
    if not str(raw).strip():
        return default
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def deliver(event, port, timeout=SOCKET_TIMEOUT):
    """POST 精简事件。失败返回 False，不抛到调用方。"""
    body = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(body) > BODY_LIMIT:
        return False
    url = 'http://127.0.0.1:%d%s' % (port, PATH)
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json; charset=utf-8',
        'Origin': 'http://127.0.0.1:%d' % port,
    })
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            resp.read(256)
        return True
    except (OSError, urllib.error.URLError, ValueError):
        return False


def read_stdin():
    """读到 EOF，只保留前 256KB 给扫描。其余丢掉，避免管道写满把 Claude Code 堵住。"""
    stream = getattr(sys.stdin, 'buffer', None)
    if stream is None:
        return b''
    try:
        if sys.stdin.isatty():
            return b''
    except Exception:
        return b''
    chunks = []
    kept = 0
    while True:
        block = stream.read(65536)
        if not block:
            break
        if kept < STDIN_SCAN_LIMIT:
            take = block[:STDIN_SCAN_LIMIT - kept]
            chunks.append(take)
            kept += len(take)
    return b''.join(chunks)


def relay_from(raw, port, timeout=SOCKET_TIMEOUT):
    event = slim_bytes(raw)
    if event is None or port is None:
        return False
    return deliver(event, port, timeout)


def main():
    try:
        port = board_port()
        if port is not None:
            relay_from(read_stdin(), port)
    except Exception:
        return 0
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
