#!/usr/bin/env python3
"""
一键安装 / 更新到 ~/.claude（Windows / macOS / Linux 通用，只依赖 Python 3.8+ 标准库）。

    python install.py            # 安装 / 更新：复制文件、合并 SessionStart 钩子、合并全局 CLAUDE.md
    python install.py --uninstall
    python ~/.claude/team-board/update.py   # 之后检查 / 一键更新（看板顶栏也有按钮）

重复运行 = 更新，不丢你的东西：
  · team.json 合并：你自建的部门 / 成员 / 形象 / 价格 / 换过的形象都保留，只补新默认项
  · 你改过的成员 .md / 技能文件不覆盖（新版本另存为 *.new 供比较）；没改过的直接更新
  · sprites/ 里导入的形象一律保留

做了什么：
  team-board/  -> ~/.claude/team-board/          （已存在的 sprites/ 会保留，不会删你本地导入的宠物）
  agents/*.md  -> ~/.claude/agents/
  skills/*     -> ~/.claude/skills/
  CLAUDE.global.md 的内容 -> 追加到 ~/.claude/CLAUDE.md（用标记包裹，可重复运行、可卸载）
  ~/.claude/settings.json -> 追加 SessionStart 钩子，路径按本机自动生成；原文件先备份为 settings.json.bak-<时间>
"""
import argparse
import os
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLAUDE = Path.home() / '.claude'
MARK_BEGIN, MARK_END = '<!-- claude-agent-team:begin -->', '<!-- claude-agent-team:end -->'
HOOK_TAG = 'team-board/ensure.py'
MANIFEST = CLAUDE / 'team-board' / '.installed.json'   # 上次安装的文件哈希：判断用户有没有改过
try:   # 所有历史版本的 agents/ skills/ 文件哈希（tools/make_dist_hashes.py 生成）：第一次升级、没有 .installed.json 时也能判断用户有没有改过
    DIST_HASHES = json.loads((HERE / 'dist-hashes.json').read_text(encoding='utf-8'))
except Exception:
    DIST_HASHES = {}


def sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding='utf-8'))
    except Exception:
        return {}


def copy_tree(src: Path, dst: Path, manifest: dict, new_manifest: dict, key: str, protect_user_edits: bool, kept: list):
    """复制目录。protect_user_edits=True 时：目标文件存在、且与上次安装的哈希不同（用户改过）、又与新文件不同 → 不覆盖，
    新版本另存为 <name>.new。team.json 与 sprites/ 单独处理。"""
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.rglob('*'):
        if f.is_dir() or '__pycache__' in f.parts:
            continue
        rel = f.relative_to(src).as_posix()
        target = dst / rel
        if key == 'team-board' and rel == 'team.json':
            continue  # 下面合并
        if key == 'team-board' and rel.startswith('sprites/') and target.exists() and sha(target) != sha(f):
            new_manifest[f'{key}/{rel}'] = sha(f)
            continue  # 用户自己放的同名形象不动
        target.parent.mkdir(parents=True, exist_ok=True)
        h = sha(f)
        if protect_user_edits and target.exists():
            old = manifest.get(f'{key}/{rel}')
            cur = sha(target)
            known = set(DIST_HASHES.get(f'{key}/{rel}', []))   # 历史上发布过的版本（未被用户改过）
            if old is not None:
                known.add(old)
            if cur != h and cur not in known:
                shutil.copy2(f, target.with_name(target.name + '.new'))
                kept.append(str(target))
                new_manifest[f'{key}/{rel}'] = old
                continue
        shutil.copy2(f, target)
        new_manifest[f'{key}/{rel}'] = h


def merge_team_json(src: Path, dst: Path, manifest: dict, new_manifest: dict):
    """team.json：新默认值打底，用户的部门 / 成员 / 形象 / 价格改动盖上去。"""
    new = json.loads(src.read_text(encoding='utf-8'))
    if not dst.exists():
        shutil.copy2(src, dst)
        new_manifest['team-board/team.json'] = sha(src)
        return
    try:
        old = json.loads(dst.read_text(encoding='utf-8'))
    except Exception:
        shutil.copy2(dst, dst.with_name('team.json.broken'))
        shutil.copy2(src, dst)
        new_manifest['team-board/team.json'] = sha(src)
        return
    out = dict(new)
    # 部门：保留用户顺序与自建部门，补上新默认部门
    if isinstance(old.get('departments'), list):
        ids = {d.get('id') for d in old['departments']}
        out['departments'] = old['departments'] + [d for d in new.get('departments', []) if d.get('id') not in ids]
    # 成员登记：默认 + 用户的（用户的覆盖同名）
    out['agents'] = {**new.get('agents', {}), **old.get('agents', {})}
    for k in ('member_pets', 'lead_pet', 'avatar_dept'):
        if k in old:
            out[k] = old[k] if k != 'avatar_dept' else {**new.get('avatar_dept', {}), **old[k]}
    # 价格：新默认为准，但用户自己加的行 / 改过的行保留（改过 = 与上次安装的默认不同）
    prev_default = manifest.get('_team_json_pricing') or {}
    pricing = dict(new.get('pricing_usd_per_mtok', {}))
    for k, v in (old.get('pricing_usd_per_mtok') or {}).items():
        if k not in pricing or (k in prev_default and prev_default[k] != v):
            pricing[k] = v
    out['pricing_usd_per_mtok'] = pricing
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    new_manifest['team-board/team.json'] = sha(dst)


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
    ap.add_argument('--no-restart', action='store_true', help='装完不重启正在跑的看板服务')
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
    manifest = load_manifest()
    new_manifest = {}
    kept = []
    copy_tree(HERE / 'team-board', CLAUDE / 'team-board', manifest, new_manifest, 'team-board', False, kept)
    merge_team_json(HERE / 'team-board' / 'team.json', CLAUDE / 'team-board' / 'team.json', manifest, new_manifest)
    copy_tree(HERE / 'agents', CLAUDE / 'agents', manifest, new_manifest, 'agents', True, kept)
    copy_tree(HERE / 'skills', CLAUDE / 'skills', manifest, new_manifest, 'skills', True, kept)
    new_manifest['_team_json_pricing'] = json.loads((HERE / 'team-board' / 'team.json').read_text(encoding='utf-8')).get('pricing_usd_per_mtok', {})
    new_manifest['_version'] = (HERE / 'VERSION').read_text(encoding='utf-8').strip() if (HERE / 'VERSION').exists() else ''
    shutil.copy2(HERE / 'VERSION', CLAUDE / 'team-board' / 'VERSION') if (HERE / 'VERSION').exists() else None
    MANIFEST.write_text(json.dumps(new_manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    merge_claude_md(True)
    merge_settings(True)
    print(f'安装完成（v{new_manifest["_version"] or "?"}）：')
    if kept:
        print('  以下文件你改过，没有覆盖；新版本放在旁边的 *.new 里供比较：')
        for k in kept:
            print('   ', k)
    print('  看板  ->', CLAUDE / 'team-board', '（新开一个 Claude Code 会话即自动常驻 http://127.0.0.1:7788/）')
    print('  成员  ->', CLAUDE / 'agents', f'（{len(list((HERE / "agents").glob("*.md")))} 位）')
    print('  技能  ->', CLAUDE / 'skills', '（agent-team）')
    print('  钩子  -> settings.json SessionStart（已备份原文件）')
    print('现在就想看看板：python', (CLAUDE / 'team-board' / 'ensure.py').as_posix())
    # 之前启动的看板进程还在跑旧代码（页面新、服务旧会出现“导入形象 not found”），装完就重启它
    if a.no_restart or os.environ.get('TEAM_BOARD_NO_RESTART'):
        return
    try:
        import subprocess
        subprocess.run([sys.executable, str(CLAUDE / 'team-board' / 'ensure.py'), '--restart'], timeout=60)
    except Exception as e:
        print('看板重启失败（不影响安装）：', e)
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
