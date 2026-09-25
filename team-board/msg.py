#!/usr/bin/env python3
"""
msg.py — 消息总线零依赖 CLI，给子代理用（看板没在跑时优雅退化到 .team/inbox/*.md）。

用法:
  python msg.py send <to> "<text>" [--from <me>] [--reply <id>]   # text 写 - 则从 stdin 读（长文 / 含引号时用）
  python msg.py inbox <me> [--peek] [--all]
  python msg.py who
  python msg.py log [-n 20]
项目按当前目录推断（服务端沿目录往上找 ~/.claude/projects 里的会话）；端口取 TEAM_BOARD_PORT（默认 7788）。
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):   # Windows 的 GBK / cp1252 控制台：打印中文不能把脚本弄崩
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

PORT = int(os.environ.get('TEAM_BOARD_PORT') or 7788)
BASE = f'http://127.0.0.1:{PORT}'

# 与 team_board.py 的 BUS_ALIASES 保持一致，供离线退化时本地规范化收件人
ALIASES = {'lead': 'lead', '__lead': 'lead', '队长': 'lead', 'main': 'lead',
           'user': 'user', '__user': 'user', '你': 'user', 'all': 'all', '*': 'all', '__all': 'all', '全员': 'all'}
SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')


def normalize_to(s: str) -> str:
    s = ' '.join(str(s or '').split())[:80]
    return ALIASES.get(s.lower(), ALIASES.get(s, s))


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机看板：绕过系统 / 环境 HTTP 代理


def _team_inbox_dir():
    """当前目录或其上级的 .team/inbox/（与服务端 find_inbox_dir 同规则），没有返回 None。"""
    here = Path.cwd()
    for d in (here, *here.parents):
        if (d / '.team' / 'inbox').is_dir():
            return d / '.team' / 'inbox'
    return None


def _cwd_q() -> str:
    return 'cwd=' + urllib.parse.quote(os.getcwd())


def _request(method: str, path: str, payload=None, timeout=5.0):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json; charset=utf-8'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def board_unreachable(exc) -> bool:
    return isinstance(exc, (urllib.error.URLError, ConnectionError, OSError, TimeoutError)) and \
        not isinstance(exc, urllib.error.HTTPError)


def _append_inbox_offline(to_type: str, frm: str, text: str, reply_to: str = '') -> str:
    """看板没在跑：直接追加 .team/inbox/<to>.md（office.md 格式），返回文件路径或 '' （目录不存在）。"""
    inbox = _team_inbox_dir()
    if inbox is None:
        return ''
    if not SAFE_NAME_RE.match(to_type) or '..' in to_type:
        return ''
    f = inbox / f'{to_type}.md'
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    extra = f'（回复 {reply_to}）' if reply_to else ''
    block = f"\n## {stamp} 来自 {frm}\n{text.strip()}{extra}\n"
    with open(f, 'a', encoding='utf-8', newline='\n') as fh:
        fh.write(block)
    return str(f)


def _read_text(arg: str) -> str:
    """text 为 - 时从 stdin 读：先按 UTF-8，解不开再按本机编码（Windows 控制台管道可能是 GBK）。"""
    if arg != '-':
        return arg
    raw = sys.stdin.buffer.read() if hasattr(sys.stdin, 'buffer') else sys.stdin.read().encode('utf-8')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        import locale
        return raw.decode(locale.getpreferredencoding(False) or 'utf-8', errors='replace')


def _fmt(m: dict) -> str:
    stamp = datetime.fromtimestamp(m.get('ts') or 0).strftime('%Y-%m-%d %H:%M')
    return f"[{stamp}] {m.get('from')} -> {m.get('to')}: {m.get('text')}  ({m.get('id', '')})"


def cmd_send(args):
    frm = args.__dict__.get('from') or os.environ.get('TEAM_AGENT_NAME') or 'unknown'
    args.text = _read_text(args.text)
    if not args.text.strip():
        print('消息是空的，没有发送。', file=sys.stderr)
        return 1
    payload = {'from': frm, 'to': args.to, 'text': args.text, 'cwd': os.getcwd(), 'via': 'cli'}
    if args.reply:
        payload['reply_to'] = args.reply
    try:
        out = _request('POST', '/api/bus/send', payload)
    except Exception as e:
        if not board_unreachable(e):
            print(f'发送失败：{e}', file=sys.stderr)
            return 1
        to = normalize_to(args.to)
        f = _append_inbox_offline(to, frm, args.text, args.reply or '')
        if f:
            print(f'看板未运行：已直接写入 {f}')
            return 0
        print('看板未运行，且当前目录没有 .team/inbox/，消息未送达。', file=sys.stderr)
        return 1
    if out.get('inbox_file'):
        print(f"已发送（同步写入 {out['inbox_file']}）：{out['message']['id']}")
    else:
        print(f"已发送：{out['message']['id']}")
    return 0


def cmd_inbox(args):
    me = args.me
    q = f'/api/bus/inbox?to={urllib.parse.quote(me)}&{_cwd_q()}'
    if args.peek:
        q += '&peek=1'
    if args.all:
        q += '&all=1'
    try:
        out = _request('GET', q)
    except Exception as e:
        if not board_unreachable(e):
            print(f'读取失败：{e}', file=sys.stderr)
            return 1
        inbox = _team_inbox_dir()
        f = (inbox or Path.cwd() / '.team' / 'inbox') / f'{normalize_to(me)}.md'
        if f.is_file():
            print(f'看板未运行：显示本地收件箱 {f}\n')
            print(f.read_text(encoding='utf-8', errors='replace'))
            return 0
        print(f'看板未运行，且 {f} 不存在。', file=sys.stderr)
        return 1
    msgs = out.get('messages') or []
    if not msgs:
        print('(没有新消息)')
    for m in msgs:
        print(_fmt(m))
    print(f"未读：{out.get('unread', 0)}")
    return 0


def cmd_who(args):
    try:
        data = _request('GET', f'/snapshot.json?{_cwd_q()}')
    except Exception as e:
        print(f'看板未运行，无法列出当前会话成员：{e}', file=sys.stderr)
        return 1
    if not data.get('session'):
        print('(当前项目没有找到活跃会话 / 成员)')
        return 0
    lead, bus = data.get('lead') or {}, data.get('bus') or {}
    print(f"{'队长':<12} {'lead':<20} {'active' if lead.get('active') else 'idle':<10} 未读:{bus.get('unreadLead', 0)}")
    for m in data.get('members') or []:
        print(f"{m.get('name', ''):<12} {m.get('agentType', ''):<20} {m.get('status', ''):<10} 未读:{m.get('unread', 0)}")
    return 0


def cmd_log(args):
    try:
        out = _request('GET', f'/api/bus?{_cwd_q()}&limit={max(1, args.n)}')
    except Exception as e:
        print(f'看板未运行，无法读取消息记录：{e}', file=sys.stderr)
        return 1
    msgs = out.get('messages') or []
    if not msgs:
        print('(没有消息)')
    for m in reversed(msgs):   # 接口最近在前；终端里按时间顺序读
        print(_fmt(m))
    return 0


def main():
    ap = argparse.ArgumentParser(prog='msg.py', description='Agent Team 消息总线 CLI')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_send = sub.add_parser('send')
    p_send.add_argument('to')
    p_send.add_argument('text', help='消息内容；写 - 从 stdin 读')
    p_send.add_argument('--from', dest='from')
    p_send.add_argument('--reply', default='')
    p_send.set_defaults(func=cmd_send)

    p_inbox = sub.add_parser('inbox')
    p_inbox.add_argument('me')
    p_inbox.add_argument('--peek', action='store_true')
    p_inbox.add_argument('--all', action='store_true')
    p_inbox.set_defaults(func=cmd_inbox)

    p_who = sub.add_parser('who')
    p_who.set_defaults(func=cmd_who)

    p_log = sub.add_parser('log')
    p_log.add_argument('-n', type=int, default=20)
    p_log.set_defaults(func=cmd_log)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
