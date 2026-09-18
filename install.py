#!/usr/bin/env python3
"""
一键安装 / 更新到 ~/.claude（Windows / macOS / Linux 通用，只依赖 Python 3.8+ 标准库）。

    python install.py            # 安装：复制文件、合并 SessionStart 钩子、合并全局 CLAUDE.md
    python install.py --uninstall

做了什么：
  team-board/  -> ~/.claude/team-board/          （已存在的 sprites/ 会保留，不会删你本地导入的宠物）
  agents/*.md  -> ~/.claude/agents/
  skills/*     -> ~/.claude/skills/
  CLAUDE.global.md 的内容 -> 追加到 ~/.claude/CLAUDE.md（用标记包裹，可重复运行、可卸载）
  ~/.claude/settings.json -> 追加 SessionStart 钩子，路径按本机自动生成；原文件先备份为 settings.json.bak-<时间>
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLAUDE = Path.home() / '.claude'
MARK_BEGIN, MARK_END = '<!-- claude-agent-team:begin -->', '<!-- claude-agent-team:end -->'
HOOK_TAG = 'team-board/ensure.py'


def copy_tree(src: Path, dst: Path):
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.rglob('*'):
        if f.is_dir() or '__pycache__' in f.parts:
            continue
        target = dst / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)


def merge_claude_md(install: bool):
    path = CLAUDE / 'CLAUDE.md'
    block = (HERE / 'CLAUDE.global.md').read_text(encoding='utf-8').strip()
    existing = path.read_text(encoding='utf-8') if path.exists() else ''
    if MARK_BEGIN in existing:
        head, rest = existing.split(MARK_BEGIN, 1)
        _, tail = rest.split(MARK_END, 1)
        existing = head.rstrip() + ('\n' + tail.lstrip() if tail.strip() else '\n')
    if install:
        existing = (existing.rstrip() + '\n\n' if existing.strip() else '') + f'{MARK_BEGIN}\n{block}\n{MARK_END}\n'
    path.write_text(existing, encoding='utf-8')


def merge_settings(install: bool):
    path = CLAUDE / 'settings.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if path.exists():
        shutil.copy2(path, path.with_name(f'settings.json.bak-{time.strftime("%Y%m%d-%H%M%S")}'))
    hooks = data.setdefault('hooks', {})
    ss = [e for e in hooks.get('SessionStart', []) if HOOK_TAG not in json.dumps(e)]
    if install:
        ensure = (CLAUDE / 'team-board' / 'ensure.py').as_posix()
        ss.append({'hooks': [{'type': 'command', 'command': 'python', 'args': [ensure], 'timeout': 15,
                              'statusMessage': '启动 Agent Team 看板…'}]})
    if ss:
        hooks['SessionStart'] = ss
    else:
        hooks.pop('SessionStart', None)
    if not hooks:
        data.pop('hooks', None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--uninstall', action='store_true')
    ap.add_argument('--no-pets', action='store_true', help='不尝试从本机 Codex 导入宠物形象')
    ap.add_argument('--petdex', action='store_true', help='再从 petdex.dev 社区画廊下载一组桌宠形象到本机（联网）')
    a = ap.parse_args()
    if sys.version_info < (3, 8):
        sys.exit('需要 Python 3.8+')
    CLAUDE.mkdir(parents=True, exist_ok=True)
    if a.uninstall:
        merge_settings(False)
        merge_claude_md(False)
        for name in (HERE / 'agents').glob('*.md'):
            (CLAUDE / 'agents' / name.name).unlink(missing_ok=True)
        for sk in (HERE / 'skills').iterdir():
            shutil.rmtree(CLAUDE / 'skills' / sk.name, ignore_errors=True)
        shutil.rmtree(CLAUDE / 'team-board', ignore_errors=True)
        print('已卸载（settings.json 与 CLAUDE.md 只移除了本工具加入的部分）')
        return
    copy_tree(HERE / 'team-board', CLAUDE / 'team-board')
    copy_tree(HERE / 'agents', CLAUDE / 'agents')
    copy_tree(HERE / 'skills', CLAUDE / 'skills')
    merge_claude_md(True)
    merge_settings(True)
    print('安装完成：')
    print('  看板  ->', CLAUDE / 'team-board', '（新开一个 Claude Code 会话即自动常驻 http://127.0.0.1:7788/）')
    print('  成员  ->', CLAUDE / 'agents', f'（{len(list((HERE / "agents").glob("*.md")))} 位）')
    print('  技能  ->', CLAUDE / 'skills', '（agent-team）')
    print('  钩子  -> settings.json SessionStart（已备份原文件）')
    print('现在就想看看板：python', (CLAUDE / 'team-board' / 'ensure.py').as_posix())
    if not a.no_pets:
        print()
        print('检测本机 Codex 客户端与 ~/.codex/pets、~/.petdex/pets，导入里面的宠物形象（只读你自己的文件，不联网）…')
        import subprocess
        r = subprocess.run([sys.executable, str(CLAUDE / 'team-board' / 'import_codex_pets.py')], capture_output=True, text=True)
        if r.returncode == 0:
            print(r.stdout.strip().splitlines()[-1])
        else:
            print('  没找到 Codex 或本机桌宠，跳过（看板用自带的 13 只形象；以后再运行 team-board/import_codex_pets.py 即可）')
    if a.petdex:
        print()
        print('从 petdex.dev 下载一组桌宠形象（作者与来源会写进 sprites/<id>.json）…')
        import subprocess
        r = subprocess.run([sys.executable, str(CLAUDE / 'team-board' / 'fetch_petdex.py'), '--starter'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()[-300:])


if __name__ == '__main__':
    main()
