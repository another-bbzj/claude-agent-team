#!/usr/bin/env python3
"""
Agent Team Board — 本地实时看板服务

借鉴 cc-haha (MIT) 的 Agent Teams 工作台思路：
直接读取 Claude Code 写在 ~/.claude/projects/<project>/<session>/subagents/ 下的
子代理 transcript (agent-*.jsonl + agent-*.meta.json)，推导每个子代理的状态与当前动作，
通过 /snapshot.json 提供给前端页面轮询。

用法:  python team_board.py [--port 7788] [--session <sessionId前缀>] [--project <projectKey子串>] [--stale 600]
"""
import argparse
import os
import sys
import io
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import hashlib
import json
import re
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

for _s in (sys.stdout, sys.stderr):   # Windows 的 GBK / cp1252 控制台：打印中文不能把脚本弄崩
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
PROJECTS = Path.home() / '.claude' / 'projects'

AVATAR_RULES = [
    ('release-engineer', r'release|build|packag|deploy|ops|ci|ship|部署|发布|打包|构建'),
    ('security-reviewer', r'secur|audit|risk|threat|review(er|s)?|审查|审校|安全|评审|复核|审阅|联调'),
    ('qa-engineer', r'qa|test|quality|verif|测试|验证|质检|用例'),
    ('docs-coordinator', r'docs?|document|product|spec|writer|coordinat|plan|readme|文档|说明|计划|规划|架构'),
    ('ui-designer', r'ui|ux|frontend|front-end|design|theme|css|style|前端|界面|设计|特效|画面|样式|动画|主题'),
    ('data-analyst', r'research|analys|investigat|explor|search|stat(s|istic)|数据分析|研究|分析|调研|搜索|查找|定位|统计'),
    ('server-engineer', r'server|backend|api|runtime|watcher|service|contract|coder|implement|algorithm|data|后端|接口|服务|模块|实现|编写|算法|数据'),
]
AVATAR_NAMES = {
    'team-lead': '队长', 'server-engineer': '阿服', 'ui-designer': '小界', 'qa-engineer': '测测',
    'security-reviewer': '老审', 'data-analyst': '探探', 'release-engineer': '发发', 'docs-coordinator': '文文',
}
AVATAR_ROLES = {
    'team-lead': '队长', 'server-engineer': '后端工程师', 'ui-designer': '界面设计师', 'qa-engineer': '测试工程师',
    'security-reviewer': '审查员', 'data-analyst': '研究分析员', 'release-engineer': '发布工程师',
    'docs-coordinator': '文档协调员',
}


TEAM_CFG = json.loads((HERE / 'team.json').read_text(encoding='utf-8'))
BUILTIN_AGENTS = set(TEAM_CFG['agents'].keys())   # 随工具发布的成员；之后登记的都算自定义
_team_cfg_mtime = (HERE / 'team.json').stat().st_mtime


def reload_team_cfg():
    global _team_cfg_mtime
    try:
        mt = (HERE / 'team.json').stat().st_mtime
        if mt != _team_cfg_mtime:
            TEAM_CFG.clear(); TEAM_CFG.update(json.loads((HERE / 'team.json').read_text(encoding='utf-8')))
            _team_cfg_mtime = mt
    except Exception:
        pass


def save_team_cfg():
    global _team_cfg_mtime
    (HERE / 'team.json').write_text(json.dumps(TEAM_CFG, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    _team_cfg_mtime = (HERE / 'team.json').stat().st_mtime


# ---- 用户自定义成员：写 ~/.claude/agents/<name>.md（Claude Code 原生格式）并把部门/形象登记到 team.json ----
AGENT_NAME_RE = re.compile(r'^[a-z][a-z0-9-]{1,40}$')
MODELS = ('sonnet', 'opus', 'haiku', 'fable', 'inherit')
EFFORTS = ('', 'low', 'medium', 'high', 'xhigh', 'max')
COLORS = ('red', 'blue', 'green', 'yellow', 'purple', 'orange', 'pink', 'cyan')
DEPT_AVATAR = {'hq': 'docs-coordinator', 'dev': 'server-engineer', 'emb': 'server-engineer', 'qa': 'qa-engineer',
               'rnd': 'data-analyst', 'docs': 'docs-coordinator', 'ops': 'release-engineer'}


DEPT_ID_RE = re.compile(r'^[a-z][a-z0-9-]{0,15}$')
REQUIRED_OPTS = ('optional', 'code', 'code2', 'deliver', 'always')


def write_department(d: dict):
    did = str(d.get('id', '')).strip()
    if not DEPT_ID_RE.match(did):
        raise ValueError('部门 id 只能用小写字母/数字/连字符，1-16 位，例如 design')
    name = ' '.join(str(d.get('name', '')).split())
    if not name:
        raise ValueError('部门名称不能为空')
    icon = str(d.get('icon', '')).strip()[:4] or '📁'
    req = str(d.get('required', 'optional')).strip() or 'optional'
    if req not in REQUIRED_OPTS:
        raise ValueError('出场规则只能是 optional / code / code2 / deliver / always')
    deps = TEAM_CFG['departments']
    entry = {'id': did, 'name': name, 'icon': icon, 'required': req}
    for i, x in enumerate(deps):
        if x['id'] == did:
            deps[i] = entry
            break
    else:
        deps.append(entry)
    save_team_cfg()
    return entry


def delete_department(did: str):
    if did == 'hq':
        raise ValueError('指挥部不能删除：队长在这里')
    deps = TEAM_CFG['departments']
    if not any(x['id'] == did for x in deps):
        raise ValueError('部门不存在')
    used = [k for k, v in TEAM_CFG['agents'].items() if v.get('dept') == did]
    if used:
        raise ValueError('部门里还有成员：' + '、'.join(used) + '。先把他们改到别的部门或删除')
    TEAM_CFG['departments'] = [x for x in deps if x['id'] != did]
    save_team_cfg()


def agent_file(name: str) -> Path:
    return AGENTS_DIRS[0] / f'{name}.md'


def read_agent(name: str):
    f = agent_file(name)
    if not AGENT_NAME_RE.match(name) or not f.exists():
        return None
    text = f.read_text(encoding='utf-8', errors='replace')
    fm = _parse_frontmatter(text)
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n?', text, re.S)
    head, body = (m.group(1), text[m.end():]) if m else ('', text)
    skills = re.findall(r'^\s*-\s*(\S+)\s*$', head.split('skills:', 1)[1], re.M) if 'skills:' in head else []
    reg = TEAM_CFG['agents'].get(name, {})
    return {'name': name, 'description': fm.get('description', ''), 'model': fm.get('model', 'inherit'),
            'effort': fm.get('effort', ''), 'color': fm.get('color', ''), 'skills': skills, 'body': body.strip(),
            'display': reg.get('name', ''), 'role': reg.get('role', ''), 'dept': reg.get('dept', ''), 'pet': reg.get('pet', ''),
            'file': str(f), 'builtin': name in BUILTIN_AGENTS}


# ---- 形象（雪碧图）管理 ---------------------------------------------------------------------------------
# 看板用 sprites/ 下的雪碧图：8 列 × ≥9 行，每帧宽高比 192:208（Codex / petdex 的桌宠格式：1536×1872 或 1536×2288，
# 也接受等比缩放过的），.webp 或 .png。每只可有同名 .json 边车记录名字、简介与出处（导入时自动写）。
SHIPPED_PETS = {'huhu-plan', 'dada-code', 'bubu-fix', 'huihui-build',                       # cc-haha（MIT）
                'pip', 'cubo', 'drip', 'mush', 'kit', 'spark', 'bolt', 'puff', 'tank'}     # make_pets.py（MIT）
PET_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')
PET_EXTS = ('webp', 'png')
PET_MAX_BYTES = 12 * 1024 * 1024
PET_COLS, PET_FRAME_W, PET_FRAME_H = 8, 192, 208
SPRITES = HERE / 'sprites'   # 导入脚本 / 测试可改


def image_size(data: bytes):
    """只读文件头拿画布尺寸（不依赖 Pillow）：webp（VP8X / VP8L / VP8）与 png。认不出返回 None。"""
    if data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:16] == b'IHDR':
        return 'png', int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        chunk = data[12:16]
        if chunk == b'VP8X':
            return 'webp', int.from_bytes(data[24:27], 'little') + 1, int.from_bytes(data[27:30], 'little') + 1
        if chunk == b'VP8L':
            b = int.from_bytes(data[21:25], 'little')
            return 'webp', (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if chunk == b'VP8 ':
            return 'webp', int.from_bytes(data[26:28], 'little') & 0x3FFF, int.from_bytes(data[28:30], 'little') & 0x3FFF
    return None


def sheet_grid(w: int, h: int):
    """校验雪碧图网格：8 列、每帧 192:208、≥9 行（允许整体等比缩放）。返回 (cw, ch, rows) 或 None。"""
    if w <= 0 or h <= 0 or w % PET_COLS:
        return None
    cw = w // PET_COLS
    ch = cw * PET_FRAME_H / PET_FRAME_W
    if abs(ch - round(ch)) > 0.01:
        return None
    ch = int(round(ch))
    if h % ch or h // ch < 9:
        return None
    return cw, ch, h // ch


def slugify_pet_id(s: str) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', str(s or '').strip().lower()).strip('-')
    return s[:41].rstrip('-')


def pet_meta_path(pet: str) -> Path:
    return SPRITES / f'{pet}.json'


def pet_info() -> dict:
    """{id: {file, w, h, cw, ch, rows, displayName?, description?, source?, credit?, shipped}}；同名时 webp 优先。"""
    out = {}
    for ext in reversed(PET_EXTS):
        for f in SPRITES.glob(f'*.{ext}'):
            if not PET_ID_RE.match(f.stem):
                continue
            try:
                with open(f, 'rb') as fh:
                    head = fh.read(64)
                info = image_size(head)
            except OSError:
                info = None
            w, h = (info[1], info[2]) if info else (PET_COLS * PET_FRAME_W, 9 * PET_FRAME_H)
            g = sheet_grid(w, h) or (PET_FRAME_W, PET_FRAME_H, 9)
            entry = {'file': f.name, 'w': w, 'h': h, 'cw': g[0], 'ch': g[1], 'rows': g[2], 'shipped': f.stem in SHIPPED_PETS}
            mp = pet_meta_path(f.stem)
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text(encoding='utf-8'))
                    for k in ('displayName', 'description', 'source', 'credit', 'license'):
                        if meta.get(k):
                            entry[k] = str(meta[k])[:300]
                    if isinstance(meta.get('fitRows'), list):
                        entry['fitRows'] = [float(x) for x in meta['fitRows']][:16]
                    if isinstance(meta.get('fit'), (int, float)):
                        entry['fit'] = float(meta['fit'])
                except Exception:
                    pass
            out[f.stem] = entry
    return out


def auto_discover_pets(force: bool = False) -> list:
    """把用户自己放进来的形象自动纳入：sprites/ 里大小写 / 下划线命名的雪碧图（改名成合法 id）、sprites/<目录>/spritesheet.*、
    ~/.codex/pets/<id>/、~/.petdex/pets/<id>/。启动时跑一次，之后每次打开成员表单 / 形象列表时再扫（只做 stat，很快）。返回新纳入的 id。"""
    global _discover_at
    now = time.time()
    if not force and now - _discover_at < 5:
        return []
    _discover_at = now
    added = []
    try:
        SPRITES.mkdir(exist_ok=True)
        # a) 文件名不合法（大写 / 下划线 / 空格）的裸雪碧图 → 改成合法 id 的副本
        for f in list(SPRITES.iterdir()):
            if f.is_file() and f.suffix.lower() in ('.webp', '.png') and '.orig' not in f.name and not PET_ID_RE.match(f.stem):
                new_id = slugify_pet_id(re.sub(r'[-_ ]?sprite(sheet)?.*$', '', f.stem, flags=re.I) or f.stem)
                if new_id and not (SPRITES / f'{new_id}{f.suffix.lower()}').exists():
                    try:
                        (SPRITES / f'{new_id}{f.suffix.lower()}').write_bytes(f.read_bytes())
                        added.append(new_id)
                    except OSError:
                        pass
        # b) 目录形式（petdex / Codex 的 pet.json + spritesheet.*）
        roots = [d for d in SPRITES.iterdir() if d.is_dir()]
        for base in (Path(os.environ.get('CODEX_HOME') or (Path.home() / '.codex')) / 'pets',
                     Path(os.environ.get('PETDEX_HOME') or (Path.home() / '.petdex')) / 'pets'):
            if base.is_dir():
                roots += [d for d in base.iterdir() if d.is_dir()]
        existing = pet_info()
        for d in roots:
            sheet = next((p for p in sorted(d.iterdir()) if p.is_file() and p.suffix.lower() in ('.webp', '.png')), None)
            if not sheet:
                continue
            meta = {}
            pj = d / 'pet.json'
            if pj.is_file():
                try:
                    meta = json.loads(pj.read_text(encoding='utf-8-sig'))
                except Exception:
                    meta = {}
            pid = slugify_pet_id(meta.get('id') or meta.get('slug') or meta.get('name') or meta.get('displayName') or d.name)
            if not pid or pid in existing or pid in SHIPPED_PETS:
                continue
            try:
                m, ext, img = parse_pet_package(sheet.read_bytes(), pid)
                m['id'] = pid
                for k in ('displayName', 'description', 'author', 'credit', 'source', 'license', 'spriteVersionNumber'):
                    if meta.get(k) and not m.get(k):
                        m[k] = meta[k]
                save_pet(m, ext, img, overwrite=False, source=str(d), credit=str(m.get('credit') or m.get('author') or ''))
                added.append(pid)
            except Exception as e:
                print(f'[pets] 跳过 {d}: {e}')
        if added:
            Handler._cache = (0.0, None)
            print('[pets] 自动纳入形象：' + '、'.join(added))
    except Exception as e:
        print(f'[pets] 自动发现失败: {e}')
    return added


_discover_at = 0.0


def pet_files() -> dict:
    return {k: v['file'] for k, v in pet_info().items()}


def pet_list() -> list:
    return sorted(pet_info().keys())


def parse_pet_package(data: bytes, hint: str = ''):
    """把用户给的桌宠文件解析成 (meta, ext, image_bytes)。
    支持：裸 .webp/.png 雪碧图；Codex / petdex 的 zip 包（pet.json + spritesheet.webp|png，可在子目录里，忽略 __MACOSX）。
    meta 至少含 id（来自 pet.json 的 id/slug/name/displayName，或文件名）。"""
    import io as _io
    import zipfile
    meta = {}
    img = None
    if data[:4] == b'PK\x03\x04':
        try:
            zf = zipfile.ZipFile(_io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ValueError('zip 包损坏')
        names = [n for n in zf.namelist() if '__MACOSX' not in n and not n.rsplit('/', 1)[-1].startswith('.')]
        js = sorted((n for n in names if n.lower().endswith('.json')), key=lambda n: (n.count('/'), 'pet.json' not in n.lower(), n))
        for n in js:
            try:
                j = json.loads(zf.read(n).decode('utf-8-sig'))
                if isinstance(j, dict) and (j.get('spritesheetPath') or j.get('displayName') or j.get('id') or j.get('name')):
                    meta = j
                    break
            except Exception:
                continue
        want = str(meta.get('spritesheetPath') or '').replace('\\', '/').rsplit('/', 1)[-1].lower()
        imgs = [n for n in names if n.lower().endswith(('.webp', '.png')) and not zf.getinfo(n).is_dir()]
        pick = next((n for n in imgs if want and n.rsplit('/', 1)[-1].lower() == want), None)
        if pick is None:
            pick = next((n for n in imgs if 'sprite' in n.lower()), None) or (max(imgs, key=lambda n: zf.getinfo(n).file_size) if imgs else None)
        if pick is None:
            raise ValueError('zip 里没有 .webp/.png 雪碧图（需要 pet.json + spritesheet.webp）')
        img = zf.read(pick)
        hint = meta.get('id') or meta.get('slug') or meta.get('name') or meta.get('displayName') or hint
    else:
        img = data
    info = image_size(img)
    if not info:
        raise ValueError('只支持 .webp / .png 雪碧图，或含它们的 zip 包')
    ext, w, h = info
    g = sheet_grid(w, h)
    if not g:
        raise ValueError(f'尺寸 {w}×{h} 不对：需要 8 列 × 至少 9 行、每帧 192:208（标准 1536×1872 或 1536×2288）')
    meta = dict(meta)
    meta['id'] = slugify_pet_id(meta.get('id') or meta.get('slug') or hint or meta.get('name') or meta.get('displayName'))
    meta['_size'] = (w, h, g[2])
    return meta, ext, img


def save_pet(meta: dict, ext: str, img: bytes, overwrite: bool = False, source: str = '', credit: str = '') -> dict:
    pet = meta.get('id') or ''
    if not PET_ID_RE.match(pet):
        raise ValueError('形象标识只能用小写字母、数字、连字符，1-40 位，例如 my-cat')
    if pet in SHIPPED_PETS:
        raise ValueError('这个标识是仓库自带形象，换一个名字')
    existing = pet_info()
    if pet in existing and not overwrite:
        raise ValueError(f'形象 {pet} 已存在；勾选覆盖或换个标识')
    SPRITES.mkdir(exist_ok=True)
    for old in PET_EXTS:  # 覆盖时把另一种扩展名的旧文件也清掉，避免 webp/png 并存
        p = SPRITES / f'{pet}.{old}'
        if p.exists() and old != ext:
            p.unlink()
    (SPRITES / f'{pet}.{ext}').write_bytes(img)
    side = {'id': pet, 'displayName': str(meta.get('displayName') or meta.get('name') or pet)[:80],
            'description': str(meta.get('description') or '')[:300],
            'source': source or str(meta.get('source') or ''), 'credit': credit or str(meta.get('credit') or meta.get('author') or meta.get('submittedBy') or ''),
            'license': str(meta.get('license') or ''), 'importedAt': datetime.now().isoformat(timespec='seconds')}
    if meta.get('spriteVersionNumber'):
        side['spriteVersionNumber'] = meta['spriteVersionNumber']
    pet_meta_path(pet).write_text(json.dumps(side, ensure_ascii=False, indent=2), encoding='utf-8')
    w, h, rows = meta.get('_size', (0, 0, 0))
    fit = fit_pet_file(pet)
    return {'id': pet, 'file': f'{pet}.{ext}', 'width': w, 'height': h, 'rows': rows, 'displayName': side['displayName'], 'fit': fit}


def fit_pet_file(pet: str, fix_cropped: bool = False):
    """用 fit_pet.py 识别主体并放大：格子里放得下就改图（留 .orig 备份），放不下的写 fitRows 让看板按行放大显示。
    需要 Pillow；没装就返回 None（前端自己用 canvas 量，同样能放大显示）。"""
    f = SPRITES / pet_files().get(pet, '')
    if not f.is_file():
        raise ValueError('形象不存在：' + pet)
    try:
        sys.path.insert(0, str(HERE))
        import fit_pet
        return fit_pet.fit_file(f, fix_cropped=fix_cropped)
    except RuntimeError as e:   # 没有 Pillow
        return {'skipped': str(e)}
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


USELESS_HINTS = {'sprite', 'sprites', 'spritesheet', 'sheet', 'pet', 'image', 'img', 'zip', 'download', 'downloads'}


def resolve_pet_id(typed: str, hints=(), overwrite: bool = False):
    """决定导入形象的标识：用户填的合法就用；填了中文 / 空 → 依次从 pet.json 的 id、文件名推；都推不出就 pet-1、pet-2…。
    只在「合法但撞了自带形象」时报错；任何名字都不会让导入失败。返回 (id, 自动改名说明或 '')。"""
    typed = str(typed or '').strip()
    slug = slugify_pet_id(typed)
    if slug and PET_ID_RE.match(slug):
        if slug in SHIPPED_PETS:
            raise ValueError(f'{slug} 是仓库自带形象的标识，换一个（或留空自动生成）')
        return slug, ('' if slug == typed else f'标识已改成 {slug}')
    existing = set(pet_info())
    for h in hints:
        hs = slugify_pet_id(h)
        if not hs or not PET_ID_RE.match(hs) or hs in USELESS_HINTS or hs in SHIPPED_PETS:
            continue
        if hs in existing and not overwrite:
            raise ValueError(f'形象 {hs} 已存在；勾选「同名覆盖」，或自己填一个别的标识')
        return hs, f'标识自动取为 {hs}'
    n = 1
    while f'pet-{n}' in existing:
        n += 1
    return f'pet-{n}', f'标识自动取为 pet-{n}'


def _finish_import(meta: dict, ext: str, img: bytes, d: dict, hints=(), source: str = '') -> dict:
    """导入的收尾：定标识 / 显示名 → save_pet。d 是 HTTP 请求体（id、displayName、overwrite、source、credit）。"""
    typed = str(d.get('id', '')).strip()
    overwrite = bool(d.get('overwrite'))
    pid, note = resolve_pet_id(typed, [meta.get('id'), meta.get('slug'), *hints, meta.get('name'), meta.get('displayName')], overwrite)
    meta = dict(meta)
    meta['id'] = pid
    display = str(d.get('displayName', '')).strip()
    if not display and typed and slugify_pet_id(typed) != pid:
        display = typed        # 填的是「菲比」这种非 ASCII 的名字：当显示名，标识另取
    if display:
        meta['displayName'] = display
    out = save_pet(meta, ext, img, overwrite=overwrite, source=source or str(d.get('source', '')), credit=str(d.get('credit', '')))
    if note:
        out['note'] = note
    return out


def _decode_b64(raw) -> bytes:
    import base64
    raw = str(raw or '')
    if raw.lstrip().startswith('data:') and ',' in raw[:200]:
        raw = raw.split(',', 1)[1]
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:
        raise ValueError('文件数据不是合法的 base64')
    if not data or len(data) > PET_MAX_BYTES:
        raise ValueError(f'文件为空或超过 {PET_MAX_BYTES // 1024 // 1024} MB')
    return data


def _hint_from_name(name: str) -> str:
    h = str(name or '').replace('\\', '/').rsplit('/', 1)[-1]
    h = re.sub(r'\.(zip|webp|png|json)$', '', h, flags=re.I)
    h = re.sub(r'[-_ ]?sprite(sheet)?.*$', '', h, flags=re.I) or h
    return h


def _merge_meta(meta: dict, extra: dict) -> dict:
    """pet.json 里的字段补进解析结果（zip 里自带的优先）。"""
    meta = dict(meta)
    for k in ('id', 'slug'):          # pet.json 说的标识比文件名推出来的准
        if extra.get(k):
            meta[k] = extra[k]
    for k in ('name', 'displayName', 'description', 'author', 'credit', 'source', 'license', 'spriteVersionNumber', 'submittedBy'):
        if extra.get(k) and not meta.get(k):
            meta[k] = extra[k]
    return meta


def write_pet(d: dict) -> dict:
    """HTTP 导入。三种给法（都不看扩展名，看文件头，所以下载下来叫 zip 没后缀也行）：
      - {data, filename?}                    一个文件：雪碧图 .webp/.png，或 Codex / petdex 的 zip 包
      - {files: [{filename, data}, ...]}     一次多选：解压出来的 pet.json + spritesheet.webp 一起选
      - {path}                               本机路径：文件夹 / zip / 图片 / pet.json（看板只监听本机，直接读）
    可选 id、displayName、overwrite、source、credit。id 填中文也行——记成显示名，标识自动推。"""
    if str(d.get('path', '')).strip():
        return import_pet_path(str(d['path']), d)
    files = d.get('files') if isinstance(d.get('files'), list) else []
    if not files and d.get('data'):
        files = [{'filename': d.get('filename') or '', 'data': d['data']}]
    if not files:
        raise ValueError('没有收到文件')
    blobs = [(str(f.get('filename') or ''), _decode_b64(f.get('data'))) for f in files if isinstance(f, dict)]
    extra, pack = {}, None
    for name, blob in blobs:
        head = blob.lstrip()[:1]
        if name.lower().endswith('.json') or head == b'{':
            try:
                j = json.loads(blob.decode('utf-8-sig'))
            except Exception:
                raise ValueError(f'{name or "pet.json"} 不是合法的 JSON')
            if isinstance(j, dict):
                extra.update(j)
        elif pack is None or (blob[:4] == b'PK\x03\x04' and pack[1][:4] != b'PK\x03\x04'):
            pack = (name, blob)
    if pack is None:
        raise ValueError('选的文件里没有雪碧图（.webp / .png）或 zip 包')
    hint = _hint_from_name(pack[0])
    meta, ext, img = parse_pet_package(pack[1], hint)
    meta = _merge_meta(meta, extra)
    return _finish_import(meta, ext, img, d, hints=[hint])


def _is_zip(f: Path) -> bool:
    try:
        with f.open('rb') as fh:
            return fh.read(4) == b'PK\x03\x04'
    except OSError:
        return False


def import_pet_path(path: str, d: dict = None) -> dict:
    """从本机路径导入：文件夹（pet.json + spritesheet.*，或里面唯一的 zip）、zip、图片、pet.json。"""
    d = d or {}
    raw = str(path).strip().strip('"\'')
    p = Path(os.path.expanduser(raw))
    if not p.exists():
        raise ValueError('路径不存在：' + raw)
    extra = {}
    if p.is_file() and p.name.lower().endswith('.json'):
        p = p.parent
    if p.is_dir():
        files = [f for f in p.iterdir() if f.is_file() and '.orig' not in f.name]
        sheets = [f for f in files if f.suffix.lower() in ('.webp', '.png')]
        zips = [f for f in files if f.suffix.lower() == '.zip' or (f.suffix == '' and _is_zip(f))]
        pick = (next((f for f in sheets if 'sprite' in f.name.lower()), None) or (max(sheets, key=lambda f: f.stat().st_size) if sheets else None)
                or (max(zips, key=lambda f: f.stat().st_size) if zips else None))
        if pick is None:
            raise ValueError(f'{raw} 里没有 .webp / .png 雪碧图，也没有 zip 包')
        pj = p / 'pet.json'
        if pj.is_file():
            try:
                j = json.loads(pj.read_text(encoding='utf-8-sig'))
                if isinstance(j, dict):
                    extra = j
            except Exception:
                pass
        hints = [_hint_from_name(pick.name), p.name]
    else:
        pick = p
        hints = [_hint_from_name(p.name), p.parent.name]
    if pick.stat().st_size > PET_MAX_BYTES:
        raise ValueError(f'{pick.name} 超过 {PET_MAX_BYTES // 1024 // 1024} MB')
    meta, ext, img = parse_pet_package(pick.read_bytes(), hints[0])
    meta = _merge_meta(meta, extra)
    return _finish_import(meta, ext, img, d, hints=hints, source=str(d.get('source') or p))


def set_member_pet(d: dict) -> dict:
    """给正在工作的成员换形象：{id, pet, scope}。scope=member 只改这一位（按会话里的成员 id 记在 team.json 的 member_pets）；
    scope=type 改这个 subagent_type 的常驻定义（以后每次都用）；scope=lead 改队长。pet 传空 = 恢复自动分配。"""
    mid = str(d.get('id', '')).strip()
    pet = str(d.get('pet', '')).strip().lower()
    scope = str(d.get('scope', 'member')).strip() or 'member'
    if pet and pet not in pet_files():
        raise ValueError('形象不存在：' + pet)
    if scope == 'lead':
        if pet:
            TEAM_CFG['lead_pet'] = pet
        else:
            TEAM_CFG.pop('lead_pet', None)
    elif scope == 'type':
        atype = str(d.get('agentType', '')).strip()
        if atype not in TEAM_CFG.get('agents', {}):
            raise ValueError('这个角色不是常驻成员（临时派的 general-purpose 只能“只改这位”）')
        if pet:
            TEAM_CFG['agents'][atype]['pet'] = pet
        else:
            TEAM_CFG['agents'][atype].pop('pet', None)
        TEAM_CFG.setdefault('member_pets', {}).pop(mid, None)
    else:
        if not re.match(r'^[A-Za-z0-9_\-]{1,80}$', mid):
            raise ValueError('成员 id 不合法')
        mp = TEAM_CFG.setdefault('member_pets', {})
        if pet:
            mp[mid] = pet
        else:
            mp.pop(mid, None)
        if len(mp) > 500:  # 只留最近 500 条，免得越积越多
            for k in list(mp)[:-500]:
                mp.pop(k, None)
    save_team_cfg()
    return {'id': mid, 'pet': pet, 'scope': scope}


def delete_pet(pet: str):
    pet = str(pet).strip().lower()
    files = pet_files()
    if pet not in files:
        raise ValueError('形象不存在')
    if pet in SHIPPED_PETS:
        raise ValueError('仓库自带的形象不能删')
    users = [k for k, v in TEAM_CFG.get('agents', {}).items() if v.get('pet') == pet]
    if users:
        raise ValueError('还有成员在用这个形象：' + '、'.join(users) + '，先给他们换形象')
    mp = TEAM_CFG.get('member_pets') or {}
    if any(v == pet for v in mp.values()):
        for k in [k for k, v in mp.items() if v == pet]:
            mp.pop(k, None)
        save_team_cfg()
    if TEAM_CFG.get('lead_pet') == pet:
        TEAM_CFG.pop('lead_pet', None); save_team_cfg()
    (SPRITES / files[pet]).unlink()
    if pet_meta_path(pet).exists():
        pet_meta_path(pet).unlink()
    for ext in PET_EXTS:   # 适配主体留下的原图备份
        b = SPRITES / f'{pet}.orig.{ext}'
        if b.exists():
            b.unlink()



AGENT_MANAGED_KEYS = {'name', 'description', 'model', 'effort', 'memory', 'color', 'skills'}


def _frontmatter_extras(text: str) -> list:
    """成员 .md 里表单不管理的 frontmatter 块（tools / permissionMode / maxTurns / hooks …），编辑时原样保留，不丢数据。"""
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n?', text, re.S)
    if not m:
        return []
    blocks, cur = [], None
    for line in m.group(1).split('\n'):
        if line and not line[0].isspace() and ':' in line:
            key = line.split(':', 1)[0].strip()
            cur = [key, [line]]
            blocks.append(cur)
        elif cur is not None:
            cur[1].append(line)
    return [ln for key, lines in blocks if key not in AGENT_MANAGED_KEYS for ln in lines]


def write_agent(d: dict):
    name = str(d.get('name', '')).strip()
    if not AGENT_NAME_RE.match(name):
        raise ValueError('标识只能用小写字母、数字、连字符，2-40 位，例如 my-reviewer')
    model = str(d.get('model', 'inherit')).strip() or 'inherit'
    if model not in MODELS and not re.match(r'^[A-Za-z0-9._:/\-]{2,80}$', model):
        raise ValueError('模型只能是 sonnet / opus / haiku / fable / inherit，或第三方完整模型 id（如 deepseek-chat、openai/gpt-5）')
    effort = str(d.get('effort', '')).strip()
    if effort not in EFFORTS:
        raise ValueError('思考强度只能是 low / medium / high / xhigh / max 或留空')
    dept = str(d.get('dept', 'dev')).strip()
    if dept not in {x['id'] for x in TEAM_CFG['departments']}:
        raise ValueError('部门不存在')
    desc = ' '.join(str(d.get('description', '')).split())
    if len(desc) < 8:
        raise ValueError('简介至少 8 个字：写清“什么时候用它”，Claude 靠这句自动委派')
    display = str(d.get('display', '')).strip() or name
    body = str(d.get('body', '')).strip() or f'你是{display}。按队长的指令工作；只改被指派的文件；完成后汇报 ≤ 200 字（产出 / 偏差 / 风险）。'
    skills = [x.strip() for x in re.split(r'[,，\s]+', str(d.get('skills', ''))) if x.strip()]
    old_text = agent_file(name).read_text(encoding='utf-8', errors='replace') if agent_file(name).exists() else ''
    old_fm = _parse_frontmatter(old_text) if old_text else {}
    lines = ['---', f'name: {name}', f'description: {desc}', f'model: {model}']
    if effort:
        lines.append(f'effort: {effort}')
    lines.append(f"memory: {old_fm.get('memory') or 'user'}")
    color = str(d.get('color', '')).strip()
    if color in COLORS:
        lines.append(f'color: {color}')
    if skills:
        lines.append('skills:')
        lines += [f'  - {x}' for x in skills]
    lines += _frontmatter_extras(old_text)
    lines.append('---')
    AGENTS_DIRS[0].mkdir(parents=True, exist_ok=True)
    agent_file(name).write_text('\n'.join(lines) + '\n' + body + '\n', encoding='utf-8')
    entry = {'dept': dept, 'avatar': DEPT_AVATAR.get(dept, 'server-engineer'), 'name': display,
             'role': str(d.get('role', '')).strip() or '自定义成员'}
    pet = str(d.get('pet', '')).strip()
    if pet and pet in pet_files():
        entry['pet'] = pet
    TEAM_CFG['agents'][name] = entry
    save_team_cfg()
    _agent_defs_cache.pop(str(agent_file(name)), None)
    return read_agent(name)


def delete_agent(name: str):
    f = agent_file(name)
    if not AGENT_NAME_RE.match(name) or not f.exists():
        raise ValueError('成员不存在')
    f.unlink()
    TEAM_CFG['agents'].pop(name, None)
    save_team_cfg()
    _agent_defs_cache.pop(str(f), None)
AGENTS_DIRS = [Path.home() / '.claude' / 'agents']
_agent_defs_cache = {}


def _parse_frontmatter(text: str) -> dict:
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.S)
    out = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        mm = re.match(r'^([A-Za-z_][\w-]*):\s*(.*?)\s*$', line)
        if mm:
            out[mm.group(1)] = mm.group(2).strip('"\'')
    return out


def load_agent_defs(extra_dirs=()):
    """读取 ~/.claude/agents/*.md（以及项目 .claude/agents）的 frontmatter：name/description/model/effort/color。"""
    defs = {}
    for d in list(AGENTS_DIRS) + list(extra_dirs):
        if not d or not d.is_dir():
            continue
        for f in sorted(d.rglob('*.md')):
            try:
                st = f.stat()
                key = (str(f), st.st_size, st.st_mtime)
                fm = _agent_defs_cache.get(str(f))
                if not fm or fm[0] != key:
                    fm = (key, _parse_frontmatter(f.read_text(encoding='utf-8', errors='replace')))
                    _agent_defs_cache[str(f)] = fm
                fm = fm[1]
                if fm.get('name'):
                    defs[fm['name']] = {'name': fm['name'], 'description': fm.get('description', ''),
                                        'model': fm.get('model', 'inherit'), 'effort': fm.get('effort', ''),
                                        'color': fm.get('color', ''), 'file': str(f),
                                        'custom': fm['name'] not in BUILTIN_AGENTS}
            except Exception:
                pass
    return defs


def model_family(model: str) -> str:
    low = (model or '').lower()
    for fam in ('opus', 'sonnet', 'haiku', 'fable'):
        if fam in low:
            return fam
    # 第三方模型（deepseek / gpt / qwen …）：去掉 provider 前缀（openai/…、deepseek/…）后取首段作为家族名，方便按家族汇总
    low = low.rsplit('/', 1)[-1]
    head = re.split(r'[-_/:.]', low, 1)[0] if low else ''
    return head or ''


def price_for(model: str):
    """从 team.json 的 pricing_usd_per_mtok 里找价格：先按最长的子串匹配（可写完整 id 或家族名），
    再退到 Claude 家族名；第三方模型没配价就返回 None（成本显示为未定价，而不是瞎算）。"""
    low = (model or '').lower()
    table = TEAM_CFG.get('pricing_usd_per_mtok') or {}
    best = None
    for key, p in table.items():
        k = key.lower()
        if k and k in low and (best is None or len(k) > len(best[0])):
            best = (k, p)
    if best:
        return best[1]
    fam = model_family(model)
    if fam in table:
        return table[fam]
    return None


def price_key(model: str):
    """price_for 实际命中的价格表 key（用于界面标注“按哪一行计价”）。"""
    low = (model or '').lower()
    table = TEAM_CFG.get('pricing_usd_per_mtok') or {}
    best = ''
    for key in table:
        k = key.lower()
        if k and k in low and len(k) > len(best):
            best = k
    if best:
        return best
    fam = model_family(model)
    return fam if fam in table else ''


def estimate_cost(model: str, usage: dict):
    p = price_for(model)
    if not p:
        return None
    return (usage.get('in', 0) * p['in'] + usage.get('out', 0) * p['out']
            + usage.get('cache_read', 0) * p['cache_read'] + usage.get('cache_write', 0) * p['cache_write']) / 1e6


def norm_path(v: str) -> str:
    return v.replace('\\', '/').lower().rstrip('/')


def short_path(v: str) -> str:
    return '/'.join(v.replace('\\', '/').split('/')[-2:])


def dept_hint(title: str):
    """任务标题开头的「研究部·」「[rnd]」「质量部：」之类 → 部门 id；没写返回 None。让队长临时派的 general-purpose 也能进对的部门。"""
    t = (title or '').strip()
    if not t:
        return None
    m = re.match(r'^[\[【（(]?\s*([^\]】）)·:：|/\-—\s]{1,16})\s*[\]】）)]?\s*[·:：|/\-—]', t)
    if not m:
        return None
    key = m.group(1).strip().lower()
    for d in TEAM_CFG['departments']:
        if key in (d['id'].lower(), d['name'].lower(), d['name'].replace('部', '').lower()):
            return d['id']
    return None


def stable_hash(s: str) -> int:
    return int(hashlib.md5(s.encode('utf-8', 'ignore')).hexdigest()[:8], 16)


def avatar_key(identity: str, agent_id: str, secondary: str = '') -> str:
    """先只看 类型+任务描述，命中即定；没命中再看指令正文；都没有则按 id 稳定散列。"""
    head = re.split(r'[：:·\-—]', identity, 1)[0] if identity else ''
    for text in (head, identity, secondary):
        low = text.lower()
        for key, pat in AVATAR_RULES:
            if low and re.search(pat, low):
                return key
    return AVATAR_RULES[stable_hash(agent_id) % len(AVATAR_RULES)][0]


def short(s, n=90):
    s = re.sub(r'\s+', ' ', str(s)).strip()
    return s if len(s) <= n else s[: n - 1] + '…'


def tool_label(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return name
    for k in ('file_path', 'path', 'notebook_path', 'command', 'pattern', 'query', 'url',
              'description', 'prompt', 'skill'):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            if k in ('file_path', 'path', 'notebook_path'):
                v = '/'.join(v.replace('\\', '/').split('/')[-3:])
            return f'{name} · {short(v, 70)}'
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return f'{name} · {short(v, 70)}'
    return name


def parse_iso(ts):
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def read_jsonl(path: Path):
    out = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
    return ''


def is_tool_result_block(content):
    return isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get('type') == 'tool_result'


_agent_cache = {}


def parse_agent(jsonl: Path, meta: dict, now: float, stale: float, group):
    try:
        st = jsonl.stat()
        mtime = st.st_mtime
        ckey = (st.st_size, st.st_mtime)
    except FileNotFoundError:
        return None
    cached = _agent_cache.get(jsonl)
    if cached and cached[0] == ckey:
        parsed = cached[1]
    else:
        parsed = _parse_agent_file(jsonl)
        _agent_cache[jsonl] = (ckey, parsed)
    return _finish_agent(parsed, jsonl, meta, now, stale, group, mtime)


def error_reason(summary: str, status: str) -> str:
    """任务通知里的失败原因 → 看板上的一句话（以前一律写成「token 上限」，用量上限 / 断网也被误报）。"""
    t = (summary or '').lower()
    if 'rate_limit' in t or '429' in t or 'usage limit' in t or 'session limit' in t:
        return '用量上限'
    if 'econnreset' in t or 'connection' in t or 'timeout' in t or 'network' in t:
        return '网络断开'
    if 'max_tokens' in t or 'token' in t and 'limit' in t:
        return '输出达到 token 上限'
    if status in ('killed', 'cancelled'):
        return '被手动停止'
    return 'API 错误'


def real_model(model) -> bool:
    """Claude Code 本地合成的消息（中断、错误回执等）model 写成 <synthetic>，不是真的模型调用，
    不能拿来覆盖成员的模型归属，否则会显示成“未定价”。"""
    return bool(model) and not str(model).startswith('<')


VERSION = next((f.read_text(encoding='utf-8').strip() for f in (HERE / 'VERSION', HERE.parent / 'VERSION') if f.exists()), '0.0.0')   # 装好后在 team-board/ 旁；仓库里直接跑时在上一级
_update_state = {'checkedAt': 0.0, 'result': None, 'busy': False, 'applying': False}


def update_check(force: bool = False) -> dict:
    """比较本机 VERSION 与 GitHub 上的；结果缓存 12 小时，检查在后台线程做，不卡快照。"""
    st = _update_state
    if st['result'] and not force and time.time() - st['checkedAt'] < 12 * 3600:
        return st['result']
    if st['busy']:
        return st['result'] or {'local': VERSION, 'remote': '', 'hasUpdate': False, 'pending': True}
    st['busy'] = True

    def run():
        try:
            import importlib
            sys.path.insert(0, str(HERE))
            up = importlib.import_module('update')
            st['result'] = up.check()
        except Exception as e:
            st['result'] = {'local': VERSION, 'remote': '', 'hasUpdate': False, 'error': f'{type(e).__name__}: {e}'}
        finally:
            st['checkedAt'] = time.time(); st['busy'] = False
    threading.Thread(target=run, daemon=True).start()
    return st['result'] or {'local': VERSION, 'remote': '', 'hasUpdate': False, 'pending': True}


def update_apply() -> dict:
    """后台起一个进程跑 update.py --apply --restart；装完它会让本服务退出并重启。"""
    if _update_state['applying']:
        return {'ok': True, 'note': '已经在更新中'}
    _update_state['applying'] = True
    import subprocess
    log = open(HERE / 'update.log', 'ab')
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
    subprocess.Popen([sys.executable, str(HERE / 'update.py'), '--apply', '--restart'], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True, cwd=str(HERE), env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    return {'ok': True, 'note': '更新已开始，约 10-30 秒后看板会自动重启；刷新页面即可'}


class UsageLedger:
    """按 message.id 去重累计 usage：同一条 API 消息会被写成多行（每个 content block 一行，usage 是当时的快照），
    只取每条消息的最后一份快照，否则 token 会被重复计 2-5 倍。"""

    def __init__(self):
        self.by_id = {}
        self.order = []
        self.anon = 0

    def add(self, msg: dict, request_id: str = ''):
        usage = msg.get('usage') or {}
        if not usage:
            return
        mid = msg.get('id') or (('req:' + request_id) if request_id else None)
        if not mid:
            self.anon += 1
            mid = f'_anon{self.anon}'
        if mid not in self.by_id:
            self.order.append(mid)
        self.by_id[mid] = usage

    def total(self):
        t = {'in': 0, 'out': 0, 'cache_read': 0, 'cache_write': 0}
        for u in self.by_id.values():
            t['in'] += u.get('input_tokens', 0) or 0
            t['out'] += u.get('output_tokens', 0) or 0
            t['cache_read'] += u.get('cache_read_input_tokens', 0) or 0
            t['cache_write'] += u.get('cache_creation_input_tokens', 0) or 0
        return t


def _parse_agent_file(jsonl: Path):
    entries = read_jsonl(jsonl)
    agent_id = jsonl.stem.replace('agent-', '')
    prompt, first_ts, last_ts = '', None, None
    timeline, tool_count, out_tokens, thinking_count = [], 0, 0, 0
    pending = {}
    last_text, last_stop, last_kind = '', None, None
    model, ledger = '', UsageLedger()
    messages, writes, reads = [], [], []

    for e in entries:
        ts = parse_iso(e.get('timestamp'))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        et = e.get('type')
        msg = e.get('message') or {}
        content = msg.get('content')
        if et == 'user':
            if not prompt and not is_tool_result_block(content):
                prompt = content_text(content)
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get('type') == 'tool_result':
                        item = pending.get(c.get('tool_use_id'))
                        if item:
                            item['done'] = True
                            item['error'] = bool(c.get('is_error'))
                            if ts and item['ts']:
                                item['ms'] = int((ts - item['ts']) * 1000)
                            if item.get('msg') is not None:
                                rtxt = content_text(c.get('content')) if isinstance(c.get('content'), list) else str(c.get('content') or '')
                                if 'not delivered' in rtxt or 'undelivered' in rtxt or 'not found' in rtxt or item['error']:
                                    item['msg']['delivered'] = False
                                    item['error'] = True
                                item.pop('msg', None)
                        last_kind = 'tool_result'
        elif et == 'assistant':
            last_stop = msg.get('stop_reason')
            ledger.add(msg, e.get('requestId') or '')
            if real_model(msg.get('model')):
                model = msg['model']
            has_tool = False
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ctype = c.get('type')
                    if ctype == 'tool_use':
                        has_tool = True
                        tool_count += 1
                        name, inp = c.get('name', '?'), c.get('input') or {}
                        item = {'ts': ts, 'kind': 'tool', 'name': name,
                                'label': tool_label(name, inp),
                                'done': False, 'error': False}
                        pending[c.get('id')] = item
                        timeline.append(item)
                        if isinstance(inp, dict):
                            if name == 'SendMessage' or name.endswith('send_message'):
                                mrec = {'ts': ts, 'to': str(inp.get('to') or inp.get('session_id') or ''), 'delivered': True,
                                        'text': short(inp.get('summary') or inp.get('message') or '', 220)}
                                messages.append(mrec)
                                item['msg'] = mrec
                            fp = inp.get('file_path') or inp.get('notebook_path')
                            if isinstance(fp, str) and fp:
                                if name in ('Write', 'Edit', 'NotebookEdit', 'MultiEdit'):
                                    writes.append({'ts': ts, 'path': norm_path(fp), 'label': short_path(fp)})
                                    inbox = re.search(r'/\.team/inbox/([^/]+)\.md$', norm_path(fp))
                                    if inbox:
                                        body = inp.get('new_string') or inp.get('content') or ''
                                        messages.append({'ts': ts, 'to': inbox.group(1), 'delivered': True, 'inbox': True,
                                                         'text': short(body, 220) or '留言'})
                                elif name == 'Read':
                                    reads.append({'ts': ts, 'path': norm_path(fp), 'label': short_path(fp)})
                    elif ctype == 'text' and c.get('text', '').strip():
                        last_text = c['text']
                        timeline.append({'ts': ts, 'kind': 'text', 'label': short(c['text'], 160)})
                    elif ctype == 'thinking':
                        thinking_count += 1
            last_kind = 'tool_use' if has_tool else 'assistant_text'

    return {'agent_id': agent_id, 'prompt': prompt, 'first_ts': first_ts, 'last_ts': last_ts, 'timeline': timeline,
            'tool_count': tool_count, 'out_tokens': out_tokens, 'thinking_count': thinking_count,
            'last_text': last_text, 'last_stop': last_stop, 'last_kind': last_kind, 'model': model,
            'usage': ledger.total(), 'messages': messages, 'writes': writes, 'reads': reads}


def _finish_agent(P, jsonl, meta, now, stale, group, mtime):
    agent_id, prompt, timeline = P['agent_id'], P['prompt'], P['timeline']
    first_ts, last_ts, last_kind, last_stop = P['first_ts'], P['last_ts'], P['last_kind'], P['last_stop']
    tool_count, out_tokens, thinking_count, last_text = P['tool_count'], P['out_tokens'], P['thinking_count'], P['last_text']

    if last_kind == 'assistant_text' and last_stop in ('end_turn', 'stop_sequence'):
        status = 'completed'
    elif last_stop == 'max_tokens':
        status = 'error'   # errorReason 缺省即「token 上限」
    elif now - mtime > stale:
        status = 'stalled'
    else:
        status = 'running'

    current = next((it for it in reversed(timeline) if it['kind'] == 'tool'), None)
    desc = meta.get('description') or short(prompt, 60) or agent_id
    atype = meta.get('agentType', '')
    known = TEAM_CFG['agents'].get(atype)
    if known:
        akey, base_name, role_title = known['avatar'], known['name'], known['role']
    else:
        akey = avatar_key(' '.join([atype, desc]), agent_id, prompt[:400])
        base_name, role_title = AVATAR_NAMES[akey], AVATAR_ROLES[akey]
    return {
        'id': agent_id,
        'avatar': akey,
        'baseName': base_name,
        'roleTitle': role_title,
        'agentType': atype,
        'model': P['model'],
        'modelFamily': model_family(P['model']),
        'usage': P['usage'],
        'cost': (lambda c: round(c, 4) if c is not None else None)(estimate_cost(P['model'], P['usage'])),
        'priced': estimate_cost(P['model'], P['usage']) is not None,
        'priceKey': price_key(P['model']),
        'messages': P['messages'],
        'writes': P['writes'],
        'reads': P['reads'],
        'toolUseId': meta.get('toolUseId'),
        'group': group,
        'description': desc,
        'prompt': short(prompt, 600),
        'status': status,
        'startedAt': first_ts,
        'lastActivityAt': mtime,
        'endedAt': last_ts if status == 'completed' else None,
        'toolCount': tool_count,
        'thinkingCount': thinking_count,
        'outputTokens': out_tokens,
        'current': current,
        'lastText': short(last_text, 400),
        'timeline': timeline[-80:],
    }


_lead_cache = {}


def _launch_cwd(cwds: list, project: str) -> str:
    """会话的启动目录：项目 key 与 transcript 所在目录名相同的那个 cwd（队长后来 cd 到别处——甚至别的会话目录——不算）。"""
    return next((c for c in cwds if project_key(c) == project), cwds[0] if cwds else '')


def parse_lead(path: Path, now: float):
    """解析队长（主会话）transcript：自己的工具调用、派工记录、SendMessage。按 (size, mtime) 缓存。"""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    key = (st.st_size, st.st_mtime)
    cached = _lead_cache.get(path)
    if cached and cached[0] == key:
        data = cached[1]
    else:
        timeline, pending, dispatches, messages, tool_count, out_tokens = [], {}, {}, [], 0, 0
        last_user_prompt, last_ts, first_ts = '', None, None
        cwds = {}   # 队长转录里出现过的 cwd → 最后出现的序号（队长会 cd 走；键顺序 = 首次出现顺序）
        notifications = {}
        lead_model, lead_ledger = '', UsageLedger()
        for i, e in enumerate(read_jsonl(path)):
            if e.get('isSidechain'):
                continue
            ts = parse_iso(e.get('timestamp'))
            if ts:
                last_ts = ts
                first_ts = first_ts or ts
            if isinstance(e.get('cwd'), str) and e['cwd']:
                cwds[e['cwd']] = i
            et = e.get('type')
            if et in ('queue-operation', 'attachment'):
                raw = json.dumps(e, ensure_ascii=False)
                if 'task-notification' in raw:
                    tid = re.search(r'<task-id>(\w+)</task-id>', raw)
                    nst = re.search(r'<status>(\w+)</status>', raw)
                    if tid and nst:
                        nsum = re.search(r'<summary>(.*?)</summary>', raw)
                        notifications[tid.group(1)] = {'status': nst.group(1), 'ts': ts, 'summary': nsum.group(1) if nsum else ''}
                continue
            msg = e.get('message') or {}
            content = msg.get('content')
            if et == 'user':
                if isinstance(content, str) and content.strip() and not content.lstrip().startswith('<'):
                    last_user_prompt = content
                elif isinstance(content, list) and not is_tool_result_block(content):
                    txt = content_text(content)
                    if txt.strip() and not txt.lstrip().startswith('<'):
                        last_user_prompt = txt
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get('type') == 'tool_result':
                            item = pending.get(c.get('tool_use_id'))
                            if item:
                                item['done'] = True
                                item['error'] = bool(c.get('is_error'))
                                if ts and item['ts']:
                                    item['ms'] = int((ts - item['ts']) * 1000)
            elif et == 'assistant':
                lead_ledger.add(msg, e.get('requestId') or '')
                if real_model(msg.get('model')):
                    lead_model = msg['model']
                if isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict) or c.get('type') != 'tool_use':
                            continue
                        name, inp, tid = c.get('name', '?'), c.get('input') or {}, c.get('id')
                        tool_count += 1
                        item = {'ts': ts, 'kind': 'tool', 'name': name, 'label': tool_label(name, inp),
                                'done': False, 'error': False}
                        pending[tid] = item
                        timeline.append(item)
                        if name == 'Agent' and isinstance(inp, dict):
                            dispatches[tid] = {'ts': ts, 'description': inp.get('description') or '',
                                               'agentType': inp.get('subagent_type') or 'general-purpose',
                                               'model': inp.get('model') or '', 'name': inp.get('name') or '',
                                               'prompt': short(inp.get('prompt') or '', 600)}
                        elif name == 'SendMessage' and isinstance(inp, dict):
                            messages.append({'ts': ts, 'kind': 'direct', 'from': '__lead', 'to': str(inp.get('to') or ''),
                                             'text': short(inp.get('summary') or inp.get('message') or '', 200)})
                        elif name in ('Write', 'Edit', 'MultiEdit') and isinstance(inp, dict) and isinstance(inp.get('file_path'), str):
                            inbox = re.search(r'/\.team/inbox/([^/]+)\.md$', norm_path(inp['file_path']))
                            if inbox:
                                body = inp.get('new_string') or inp.get('content') or ''
                                messages.append({'ts': ts, 'kind': 'direct', 'from': '__lead', 'to': inbox.group(1),
                                                 'text': '留言：' + short(body, 200)})
        lead_usage = lead_ledger.total()
        out_tokens = lead_usage['out']
        data = {'timeline': timeline[-60:], 'toolCount': tool_count, 'outputTokens': out_tokens,
                'model': lead_model, 'usage': lead_usage, 'cost': (lambda c: round(c, 4) if c is not None else None)(estimate_cost(lead_model, lead_usage)),
                'dispatches': dispatches, 'messages': messages, 'notifications': notifications,
                'lastUserPrompt': short(last_user_prompt, 300),
                'lastTs': last_ts, 'firstTs': first_ts, 'mtime': st.st_mtime,
                'cwd': _launch_cwd(list(cwds), path.parent.name), 'cwds': sorted(cwds, key=lambda c: -cwds[c])}
        _lead_cache[path] = (key, data)
    current = next((it for it in reversed(data['timeline']) if it['kind'] == 'tool'), None)
    active = now - data['mtime'] < 20
    return {**data, 'current': current, 'active': active}


def discover_sessions():
    found = []
    if not PROJECTS.is_dir():
        return found
    for proj in PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        for sess in proj.iterdir():
            sub = sess / 'subagents'
            if not (sess.is_dir() and sub.is_dir()):
                continue
            files = list(sub.glob('agent-*.jsonl')) + list(sub.glob('workflows/*/agent-*.jsonl'))
            if not files:
                continue
            mt = max(f.stat().st_mtime for f in files)
            found.append({'project': proj.name, 'session': sess.name, 'dir': sub, 'mtime': mt, 'count': len(files)})
    found.sort(key=lambda x: -x['mtime'])
    return found


# ---- 消息总线（bus）：成员之间 / 看板与成员之间的消息。bus/messages.jsonl 追加写 + bus/cursors.json 已读游标 ----
BUS = HERE / 'bus'   # 测试可改
BUS_MAX_TEXT = 4000
BUS_ALIASES = {'lead': 'lead', '__lead': 'lead', '队长': 'lead', 'main': 'lead',
               'user': 'user', '__user': 'user', '你': 'user', 'all': 'all', '*': 'all', '__all': 'all', '全员': 'all'}
BUS_VIA = ('cli', 'board', 'api')
SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')   # 收件箱文件名 / 成员 id：不许 / \ ..
_bus_lock = threading.RLock()
_bus_cache = {'key': None, 'msgs': []}


PROJECT_RE = re.compile(r'^[A-Za-z0-9_.-]{1,200}$')


def project_key(cwd: str) -> str:
    """Claude Code 的项目 key：~/.claude/projects/<key> 的目录名。"""
    return re.sub(r'[^A-Za-z0-9]', '-', str(cwd or '')) if cwd else ''


def _ancestors(cwd: str) -> list:
    try:
        p = Path(cwd)
        return [p, *p.parents] if p.is_absolute() else []
    except (TypeError, ValueError):
        return []


def resolve_project(cwd: str) -> str:
    """cwd → 会话的项目 key。子代理的 cwd 常是队长启动目录的子目录（队长 cd 过去、或在 .team/ 里干活），
    所以沿 cwd 往上找 ~/.claude/projects 里存在的 key，取最近有活动的那个；都没有就退回 project_key(cwd)。"""
    best, best_mt = '', -1.0
    for d in _ancestors(cwd):
        k = project_key(str(d))
        pd = PROJECTS / k
        if not PROJECT_RE.match(k) or not pd.is_dir():
            continue
        mt = max((f.stat().st_mtime for f in pd.glob('*.jsonl')), default=0.0)
        if mt > best_mt:
            best, best_mt = k, mt
    return best or _project_by_real_cwd(cwd) or project_key(cwd)


_proj_cwd_cache = {}   # 项目目录 → ((最新转录 mtime, 路径), 真实 cwd)


def _real(p: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(p))
    except (OSError, ValueError):
        return ''


def _project_by_real_cwd(cwd: str) -> str:
    """按真实路径匹配：项目在符号链接目录下时（macOS 的 /var → /private/var、自己建的软链），
    转录里记的 cwd 和成员 os.getcwd() 拿到的路径字面不同，key 对不上。读每个项目最新转录里的 cwd，
    realpath 后看它是不是请求 cwd 的祖先（或相同），取最近有活动的那个。"""
    rc = _real(cwd)
    if not rc or not PROJECTS.is_dir():
        return ''
    best, best_mt = '', -1.0
    for pd in PROJECTS.iterdir():
        if not pd.is_dir():
            continue
        try:
            latest = max(pd.glob('*.jsonl'), key=lambda f: f.stat().st_mtime, default=None)
        except OSError:
            continue
        if latest is None:
            continue
        mt = latest.stat().st_mtime
        key = (mt, str(latest))
        cached = _proj_cwd_cache.get(pd)
        if cached and cached[0] == key:
            pcwd = cached[1]
        else:
            pcwd = ''
            for e in read_jsonl(latest):
                if isinstance(e.get('cwd'), str) and e['cwd']:
                    pcwd = _real(e['cwd'])
                    break
            _proj_cwd_cache[pd] = (key, pcwd)
        if pcwd and (rc == pcwd or rc.startswith(pcwd.rstrip(os.sep) + os.sep)) and mt > best_mt:
            best, best_mt = pd.name, mt
    return best


def find_inbox_dir(cwds) -> 'Path | None':
    """按优先顺序对每个 cwd 往上找 .team/inbox/，第一个存在的就是收件箱目录。"""
    for c in cwds:
        for d in _ancestors(c):
            if (d / '.team' / 'inbox').is_dir():
                return d / '.team' / 'inbox'
    return None


def bus_party(s) -> str:
    """规范化收发人：队长 / 用户 / 全员的同义词 → lead / user / all；其余原样（去空白，≤80 字）。"""
    s = ' '.join(str(s or '').split())[:80]
    return BUS_ALIASES.get(s.lower(), BUS_ALIASES.get(s, s))


def bus_messages() -> list:
    """读全部消息（按 (size, mtime) 缓存）。"""
    f = BUS / 'messages.jsonl'
    try:
        st = f.stat()
    except FileNotFoundError:
        return []
    key = (str(f), st.st_size, st.st_mtime)
    with _bus_lock:
        if _bus_cache['key'] != key:
            _bus_cache['msgs'] = [m for m in read_jsonl(f) if isinstance(m, dict) and m.get('id')]
            _bus_cache['key'] = key
        return _bus_cache['msgs']


def _bus_cursors() -> dict:
    try:
        j = json.loads((BUS / 'cursors.json').read_text(encoding='utf-8'))
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def find_session(prefix: str):
    """会话 id 前缀 → (project key, 队长 transcript 路径)；找不到返回 (None, None)。"""
    prefix = str(prefix or '').strip()
    if not re.match(r'^[A-Za-z0-9_-]{1,80}$', prefix) or not PROJECTS.is_dir():
        return None, None
    best = None
    for proj in PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob(prefix + '*.jsonl'):
            if best is None or f.stat().st_mtime > best.stat().st_mtime:
                best = f
        for d in proj.glob(prefix + '*'):   # 只有 subagents 目录、队长 transcript 还没写出的情况
            if best is None and d.is_dir():
                best = d.with_suffix('.jsonl')
    return (best.parent.name, best) if best else (None, None)


def _resolve_agent_type(to: str, project: str = '') -> str:
    """收件人 → subagent_type（收件箱文件名）：常驻成员标识 / 显示名（可带 ·2）/ 会话里的成员 id。认不出且本身是安全文件名就原样用。"""
    agents = TEAM_CFG.get('agents') or {}
    if to in agents:
        return to
    base = re.sub(r'·\d+$', '', to)
    for k, v in agents.items():
        if base and base in (v.get('name'), k):
            return k
    if re.match(r'^[A-Za-z0-9_-]{1,80}$', to) and project and PROJECT_RE.match(project) and (PROJECTS / project).is_dir():
        for meta in (PROJECTS / project).glob(f'*/subagents/agent-{to}.meta.json'):
            try:
                at = json.loads(meta.read_text(encoding='utf-8')).get('agentType') or ''
                if at:
                    return at
            except Exception:
                pass
    return to if SAFE_NAME_RE.match(to) and '..' not in to else ''


def _append_inbox(cwds: list, to_type: str, msg: dict):
    """cwd（或其上级）有 .team/inbox/ 时按 office.md 格式追加一段；返回文件路径或 None。"""
    if not to_type or not SAFE_NAME_RE.match(to_type) or '..' in to_type:
        return None
    inbox = find_inbox_dir(cwds)
    if inbox is None:
        return None
    f = inbox / f'{to_type}.md'
    stamp = datetime.fromtimestamp(msg['ts']).strftime('%Y-%m-%d %H:%M')
    extra = f"（回复 {msg['reply_to']}）" if msg.get('reply_to') else ''
    block = f"\n## {stamp} 来自 {msg['from']}\n{msg['text'].strip()}{extra}\n<!-- bus:{msg['id']} -->\n"
    with open(f, 'a', encoding='utf-8', newline='\n') as fh:
        fh.write(block)
    return str(f)


def bus_send(d: dict) -> dict:
    """POST /api/bus/send。返回 {ok, message, inbox_file?}；参数不对抛 ValueError（400）。"""
    text = str(d.get('text') or '').replace('\r\n', '\n').strip()
    to = bus_party(d.get('to'))
    frm = bus_party(d.get('from')) or 'unknown'
    if not to:
        raise ValueError('to（收件人）不能为空')
    if not text:
        raise ValueError('text（消息内容）不能为空')
    if len(text) > BUS_MAX_TEXT:
        raise ValueError(f'消息太长：{len(text)} 字，上限 {BUS_MAX_TEXT}')
    project, cwd = str(d.get('project') or '').strip(), str(d.get('cwd') or '').strip()
    cwds = [cwd] if cwd else []
    if d.get('session'):
        sp, lead_path = find_session(d['session'])
        if sp:
            project = sp
            if not cwd and lead_path and lead_path.exists():   # 看板发的：队长最近待过的目录优先找 .team/inbox/
                cwds = (parse_lead(lead_path, time.time()) or {}).get('cwds') or []
    if cwd and not project:
        project = resolve_project(cwd)
    if project and not PROJECT_RE.match(project):
        raise ValueError('project 不合法')
    via = str(d.get('via') or '')
    via = via if via in BUS_VIA else ('board' if frm == 'user' else 'api')
    to_type = '' if to in ('user', 'all') else ('lead' if to == 'lead' else _resolve_agent_type(to, project))
    with _bus_lock:
        BUS.mkdir(parents=True, exist_ok=True)
        prev = bus_messages()
        ts = round(max(time.time(), (prev[-1].get('ts') or 0) + 0.001 if prev else 0), 3)   # 单调递增，游标按 ts 比较
        msg = {'id': f"m-{int(ts * 1000)}-{os.urandom(2).hex()}", 'ts': ts, 'from': frm, 'to': to, 'text': text,
               'project': project, 'via': via}
        if to_type and to_type != to:
            msg['toType'] = to_type   # to 是成员 id / 显示名时，记下解析出的 subagent_type，收件箱按它也能收到
        if d.get('reply_to'):
            msg['reply_to'] = str(d['reply_to'])[:40]
        with open(BUS / 'messages.jsonl', 'a', encoding='utf-8', newline='\n') as fh:
            fh.write(json.dumps(msg, ensure_ascii=False) + '\n')
    out = {'ok': True, 'message': msg}
    if to_type:
        try:
            f = _append_inbox(cwds, to_type, msg)
            if f:
                out['inbox_file'] = f
        except OSError as e:
            out['inbox_error'] = str(e)
    return out


def _bus_scope(msgs, project: str):
    return [m for m in msgs if not project or not m.get('project') or m.get('project') == project]


def _bus_for(msgs, me: str, aliases=()):
    """发给 me（或其别名）或全员、且不是 me 自己发的消息。"""
    names = {me, *aliases} - {''}
    return [m for m in msgs if (m.get('to') in names or m.get('toType') in names or m.get('to') == 'all')
            and m.get('from') not in names]


def _cursor_of(cur: dict, me: str, project: str) -> float:
    return max(float(cur.get(f'{me}|{project}') or 0), float(cur.get(f'{me}|') or 0))


def bus_inbox(to: str, project: str = '', peek: bool = False, everything: bool = False) -> dict:
    """GET /api/bus/inbox。默认只返回未读并推进游标。"""
    me = bus_party(to)
    if not me:
        raise ValueError('to 不能为空')
    with _bus_lock:
        mine = _bus_for(_bus_scope(bus_messages(), project), me, [_resolve_agent_type(me, project)] if me not in ('lead', 'user', 'all') else [])
        cur = _bus_cursors()
        seen = _cursor_of(cur, me, project)
        unread = [m for m in mine if (m.get('ts') or 0) > seen]
        if not peek and unread:
            cur[f'{me}|{project}'] = max(m.get('ts') or 0 for m in unread)
            BUS.mkdir(parents=True, exist_ok=True)
            tmp = BUS / 'cursors.json.tmp'
            tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding='utf-8')
            os.replace(tmp, BUS / 'cursors.json')
    return {'messages': mine if everything else unread, 'unread': len(unread)}


def bus_unread(me: str, project: str, aliases=(), msgs=None, cur=None) -> int:
    msgs = _bus_scope(bus_messages() if msgs is None else msgs, project)
    seen = _cursor_of(_bus_cursors() if cur is None else cur, me, project)
    return sum(1 for m in _bus_for(msgs, me, aliases) if (m.get('ts') or 0) > seen)


def bus_log(project: str = '', limit: int = 200, session: str = '') -> dict:
    if session and not project:
        project = find_session(session)[0] or ''
    msgs = _bus_scope(bus_messages(), project)
    return {'messages': list(reversed(msgs[-max(1, min(int(limit or 200), 2000)):]))}


# ---- 背景板（Wallpaper Engine 壁纸 / 当前桌面 / 上传图）：backdrops/backdrop.json + 本机图片；
# 内置立绘预设已移除（v1.4.1），不再联网下载任何图片。
# v1 字段（file/credit/opacity/side/enabled）逻辑在 backdrop_store.py；v2 尺寸/透明化/壁纸字段在这里管理
# （backdrop_store.clean_config 只认 v1 字段，写回会把 v2 字段丢掉，所以这里绕开它直接读写 backdrop.json）。
BACKDROPS = HERE / 'backdrops'   # 测试可改
_bs_mod = None
_wp_mod = None

BACKDROP_DEFAULTS_V2 = {
    'kind': 'image', 'source': '', 'title': '', 'wallpaperId': '',
    'fit': 'contain', 'scale': 100, 'x': 50, 'y': 50, 'area': 'page',
    'blur': 0, 'dim': 0, 'saturate': 1, 'mask': 'none', 'blend': 'normal', 'panelAlpha': 1.0,
}
BACKDROP_ENUMS = {
    'kind': ('image', 'video'), 'area': ('page', 'stage'), 'fit': ('contain', 'cover', 'custom'),
    'mask': ('none', 'fade-left', 'fade-right', 'vignette', 'fade-bottom'),
    'blend': ('normal', 'luminosity', 'screen', 'multiply', 'soft-light'),
}
BACKDROP_RANGES = {'scale': (10, 400), 'x': (0, 100), 'y': (0, 100),
                    'blur': (0, 20), 'dim': (0, 0.9), 'saturate': (0, 2), 'panelAlpha': (0.2, 1)}


class BackdropFetchError(Exception):
    """预设下载失败（网络 / 站点）：接口回 502。"""


def _bs():
    """按文件路径加载本目录的 backdrop_store.py（不走 sys.modules 缓存，免得测试里串到别的副本）。"""
    global _bs_mod
    if _bs_mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location('backdrop_store_' + str(abs(hash(str(HERE)))), HERE / 'backdrop_store.py')
        _bs_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_bs_mod)
    return _bs_mod


def _wp():
    """按文件路径加载本目录的 wallpapers.py（同 _fb()，不走 sys.modules 缓存）。"""
    global _wp_mod
    if _wp_mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location('wallpapers_' + str(abs(hash(str(HERE)))), HERE / 'wallpapers.py')
        _wp_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_wp_mod)
    return _wp_mod


def _read_backdrop_raw(root: Path) -> dict:
    try:
        j = json.loads((root / 'backdrop.json').read_text(encoding='utf-8'))
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def _write_backdrop_raw(cfg: dict, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / 'backdrop.json.tmp'
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, root / 'backdrop.json')


def _clean_backdrop_v2(raw: dict, base: dict) -> dict:
    """v1 之外的新字段：独立清洗、夹到取值范围内；未知/越界值一律回落默认值。"""
    out = dict(BACKDROP_DEFAULTS_V2)
    for k in out:
        if k in raw:
            out[k] = raw[k]
    if 'x' not in raw and 'side' in raw:   # 兼容 1 版：只有旧字段时 side=right → x=100，left → x=0
        out['x'] = 100 if base.get('side') == 'right' else 0
    for k, choices in BACKDROP_ENUMS.items():
        if out[k] not in choices:
            out[k] = BACKDROP_DEFAULTS_V2[k]
    for k, (lo, hi) in BACKDROP_RANGES.items():
        try:
            out[k] = round(min(hi, max(lo, float(out[k]))), 3)
        except (TypeError, ValueError):
            out[k] = BACKDROP_DEFAULTS_V2[k]
    out['title'] = str(out.get('title') or '')[:200]
    out['source'] = str(out.get('source') or '')[:40]
    out['wallpaperId'] = str(out.get('wallpaperId') or '')[:200]
    return out


def backdrop_state(presets: bool = True) -> dict:
    bs = _bs()
    raw = _read_backdrop_raw(BACKDROPS)
    base = bs.clean_config(raw)
    v2 = _clean_backdrop_v2(raw, base)
    url = ''
    if v2['kind'] == 'video' and v2['wallpaperId'] and _wp().resolve_media(v2['wallpaperId'], 'media'):
        url = f"/api/wallpapers/{v2['wallpaperId']}/media"
    elif base['file'] and (BACKDROPS / base['file']).is_file():
        url = f"/backdrops/{base['file']}?v={int((BACKDROPS / base['file']).stat().st_mtime)}"
    # 注意：不用 base['enabled']——bs.clean_config 要求 file 非空才算 enabled，而 video 壁纸不落 file（引用外部源）
    out = {'enabled': bool(raw.get('enabled')) and bool(url), 'url': url, 'credit': base['credit'],
           'opacity': base['opacity'], 'side': base['side'], **v2}
    out.pop('wallpaperId', None)   # 内部字段（引用外部视频用），前端不需要
    if presets:
        out['presets'] = []   # v1.4.1：内置立绘预设已移除，恒为空数组（字段保留只为兼容旧前端）
    return out


def write_backdrop(d: dict) -> dict:
    """POST /api/backdrop：
    换图：{data[,name]} / {path} / {wallpaper:<id>}（WE 壁纸） / {desktop:true}（当前桌面快照）
    改设置：{opacity, side, enabled, credit} 及 v2 字段（kind/source/title/fit/scale/x/y/area/blur/dim/saturate/mask/blend/panelAlpha）
    关闭：{clear:true}（删图）。以上可与设置组合。
    v1.4.1：{preset} 已移除（不再内置任何立绘预设），一律 400；旧配置里 source:'preset' 的图片本身不受影响，仍会正常显示。"""
    bs = _bs()
    raw = _read_backdrop_raw(BACKDROPS)
    extra = {}   # 本次响应里附带的临时信息（quality/note/warning），不落盘

    if d.get('clear'):
        f = str(raw.get('file') or '')
        if bs.FILE_RE.match(f) and (BACKDROPS / f).is_file():
            (BACKDROPS / f).unlink()
        raw.update({'file': '', 'enabled': False, 'credit': '', 'kind': 'image', 'source': '', 'wallpaperId': '', 'title': ''})
        _write_backdrop_raw({**bs.clean_config(raw), **_clean_backdrop_v2(raw, bs.clean_config(raw))}, BACKDROPS)
        return backdrop_state()

    if d.get('preset'):
        raise ValueError('内置立绘预设已移除，请用 Wallpaper Engine / 当前桌面 / 上传')
    elif d.get('data'):
        b64 = str(d['data'])
        if b64.lstrip().startswith('data:') and ',' in b64[:200]:
            b64 = b64.split(',', 1)[1]
        if len(b64) > bs.MAX_BYTES * 4 // 3 + 16:
            raise ValueError('图片超过 15 MB')
        import base64
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception:
            raise ValueError('图片数据不是合法的 base64')
        cfg = bs.save_image(data, 'custom', BACKDROPS, credit=str(d.get('credit') or d.get('name') or '本机图片'))
        raw.update(cfg)
        raw.update({'kind': 'image', 'source': 'upload', 'wallpaperId': '', 'title': str(d.get('name') or '')})
    elif str(d.get('path') or '').strip():
        p = Path(os.path.expanduser(str(d['path']).strip().strip('"\'')))
        if not p.is_file():
            raise ValueError('文件不存在：' + str(p))
        if p.stat().st_size > bs.MAX_BYTES:
            raise ValueError('图片超过 15 MB')
        cfg = bs.save_image(p.read_bytes(), 'custom', BACKDROPS, credit=str(d.get('credit') or p.name))
        raw.update(cfg)
        raw.update({'kind': 'image', 'source': 'path', 'wallpaperId': '', 'title': p.name})
    elif str(d.get('wallpaper') or '').strip():
        wp = _wp()
        wid = str(d['wallpaper']).strip()
        item = wp.find_item(wid)
        if not item:
            raise ValueError('未知的壁纸：' + wid)
        if item['_mediaKind'] == 'video':
            raw.update({'file': '', 'enabled': True, 'credit': item['title'],
                        'kind': 'video', 'source': 'wallpaper', 'wallpaperId': wid, 'title': item['title']})
            info = _wp().probe_video(item['_media'])
            extra['quality'] = 'full'
            extra['note'] = info.get('note') or ''
            if info.get('playable') is False:
                # 仍然允许应用（用户可能装了对应解码扩展），只是提示可能放不了
                extra['warning'] = info.get('note') or '该视频可能无法在浏览器中播放'
        else:
            stem = 'we-' + (re.sub(r'[^a-z0-9]+', '', wid.lower())[:24] or 'wp')
            full = _wp().get_scene_full_image(wid) if item['type'] == 'scene' else None
            cfg = None
            if full:
                img_path, _w, _h = full
                try:
                    cfg = bs.save_image(img_path.read_bytes(), stem, BACKDROPS, credit=item['title'])
                    extra['quality'] = 'full'
                except ValueError:
                    cfg = None   # 原图太大等失败（如 > 15MB）：退回 preview，不整体报错
            if cfg is None:
                src = item.get('_preview')
                if not src or not src.is_file():
                    raise ValueError('该壁纸没有可用的预览图（3D 场景 / web 壁纸只能用预览，可先设为桌面壁纸再用「当前桌面」拿高清图）')
                cfg = bs.save_image(src.read_bytes(), stem, BACKDROPS, credit=item['title'])
                extra['quality'] = 'preview'
            raw.update(cfg)
            raw.update({'kind': 'image', 'source': 'wallpaper', 'wallpaperId': '', 'title': item['title']})
    elif d.get('desktop'):
        src = _wp().desktop_wallpaper_path()
        if not src:
            raise ValueError('没有可用的桌面壁纸快照（非 Windows，或 Wallpaper Engine 未写入）')
        cfg = bs.save_image(src.read_bytes(), 'desktop', BACKDROPS, credit='当前桌面壁纸')
        raw.update(cfg)
        raw.update({'kind': 'image', 'source': 'desktop', 'wallpaperId': '', 'title': '当前桌面壁纸'})

    keys = {k: d[k] for k in ('opacity', 'side', 'enabled', 'credit') if k in d}
    if 'side' in keys and keys['side'] not in ('left', 'right'):
        raise ValueError('side 只能是 left / right')
    if 'opacity' in keys:
        try:
            float(keys['opacity'])
        except (TypeError, ValueError):
            raise ValueError('opacity 要是 0-1 的数字')
    raw.update(keys)

    v2keys = {k: d[k] for k in BACKDROP_DEFAULTS_V2 if k in d}
    for k, choices in BACKDROP_ENUMS.items():
        if k in v2keys and v2keys[k] not in choices:
            raise ValueError(f'{k} 取值不合法（可选：{"/".join(choices)}）')
    for k in BACKDROP_RANGES:
        if k in v2keys:
            try:
                float(v2keys[k])
            except (TypeError, ValueError):
                raise ValueError(f'{k} 必须是数字')
    raw.update(v2keys)

    base = bs.clean_config(raw)
    v2 = _clean_backdrop_v2(raw, base)
    has_source = bool(base['file'] or v2['wallpaperId'])
    if raw.get('enabled') and not has_source:
        raise ValueError('还没有背景图：先上传，或选一个壁纸 / 当前桌面')
    # base['enabled']（backdrop_store.clean_config 算的）要求 file 非空，video 壁纸没有 file 会被它强制关掉，
    # 这里用真正的意图值覆盖回去，backdrop_state 读的也是这个 raw 值而不是 base['enabled']。
    final = {**base, **v2, 'enabled': bool(raw.get('enabled')) and has_source}
    _write_backdrop_raw(final, BACKDROPS)
    return {**backdrop_state(), **extra}


def build_snapshot(args):
    reload_team_cfg()
    now = time.time()
    sessions = discover_sessions()
    if args.session:
        chosen = next((s for s in sessions if s['session'].startswith(args.session)), None)
    elif args.project:   # 先精确匹配项目 key（msg.py who），再按子串（--project 命令行参数）
        chosen = next((s for s in sessions if s['project'] == args.project), None)             or next((s for s in sessions if args.project in s['project']), None)
    else:
        chosen = sessions[0] if sessions else None

    members, session_info = [], None
    if chosen:
        sub = chosen['dir']
        files = [(f, None) for f in sorted(sub.glob('agent-*.jsonl'))]
        for wf in sorted(sub.glob('workflows/*')):
            files += [(f, wf.name) for f in sorted(wf.glob('agent-*.jsonl'))]
        for f, group in files:
            meta_path = f.with_name(f.stem + '.meta.json')
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding='utf-8'))
                except Exception:
                    meta = {}
            members.append(parse_agent(f, meta, now, args.stale, group))
        members.sort(key=lambda m: (m['startedAt'] or 0))
        seen = {}
        for m in members:
            seen[m['avatar']] = seen.get(m['avatar'], 0) + 1
            m['name'] = m['baseName'] if seen[m['avatar']] == 1 else f"{m['baseName']}·{seen[m['avatar']]}"
        session_info = {'id': chosen['session'], 'project': chosen['project'], 'lastActivity': chosen['mtime']}
        lead_path = sub.parent.parent / (chosen['session'] + '.jsonl')
        lead_data = parse_lead(lead_path, now)
        session_info['cwd'] = (lead_data or {}).get('cwd') or ''
    else:
        lead_data = None
    members = [m for m in members if m]

    # 部门归属 + agent 定义（model/effort 来自 ~/.claude/agents/*.md）
    agent_defs = load_agent_defs()
    for m in members:
        cfg = TEAM_CFG['agents'].get(m['agentType'])
        d = agent_defs.get(m['agentType'], {})
        m['dept'] = cfg['dept'] if cfg else (dept_hint(m.get('description', '')) or TEAM_CFG['avatar_dept'].get(m['avatar'], 'dev'))
        m['pet'] = (TEAM_CFG.get('member_pets') or {}).get(m['id']) or (cfg or {}).get('pet', '')
        m['petScope'] = 'member' if (TEAM_CFG.get('member_pets') or {}).get(m['id']) else ('type' if (cfg or {}).get('pet') else '')
        m['hasDef'] = bool(cfg)
        m['definedModel'] = d.get('model', '')
        m['effort'] = d.get('effort', '')
        m['custom'] = bool(d.get('custom'))
        if not m['model'] and m['definedModel'] and m['definedModel'] != 'inherit':
            m['model'] = m['definedModel']
            m['modelFamily'] = model_family(m['model'])

    # 用 meta.toolUseId 把队长的 Agent 调用和成员对上，得到派工时间与指令
    comms = []
    if lead_data:
        for m in members:
            n = lead_data['notifications'].get(m['id'])
            if n:
                if n['status'] == 'completed' and m['status'] != 'completed':
                    m['status'] = 'completed'
                    m['endedAt'] = m['endedAt'] or n['ts'] or m['lastActivityAt']
                elif n['status'] in ('failed', 'error', 'killed', 'cancelled') and m['status'] != 'completed':
                    m['status'] = 'error'
                    m['errorReason'] = error_reason(n.get('summary', ''), n['status'])
            elif m['status'] == 'completed' and now - m['lastActivityAt'] < 8:
                m['status'] = 'running'  # 刚写完总结、队长还没收到通知的几秒内，仍视作进行中
            d = lead_data['dispatches'].get(m.get('toolUseId'))
            if d:
                m['dispatchedAt'] = d['ts']
                m['dispatchName'] = d.get('name') or ''
                m['dispatchModel'] = d.get('model') or ''
                if not m['agentType']:
                    m['agentType'] = d['agentType']
                comms.append({'ts': d['ts'], 'kind': 'assign', 'from': '__lead', 'fromName': '队长', 'fromAvatar': 'team-lead',
                              'to': m['id'], 'toName': m['name'], 'toAvatar': m['avatar'], 'text': d['description'] or m['description']})
            # 队长转达：派工指令里点名了先前成员（如“阿服·2 已写好 store.js”）→ 画一条 该成员 → 新成员 的转达线
            if not d:
                continue  # 没对上派工记录（meta 缺 toolUseId 或来自旧会话）
            ptxt = re.sub('你是[^。，,\\n]{0,16}', '', d.get('prompt') or '')  # 去掉“你是研发部的阿服”这类自我角色描述
            seen_base = set()
            for other in sorted(members, key=lambda x: -(x.get('dispatchedAt') or x.get('startedAt') or 0)):
                if other['id'] == m['id'] or (other.get('dispatchedAt') or 0) > (d['ts'] or 0):
                    continue
                if other['baseName'] == m['baseName'] and '·' not in other['name']:
                    continue
                if other['baseName'] in seen_base:
                    continue
                if len(other['name']) >= 2 and re.search('(?:' + re.escape(other['name']) + '|' + re.escape(other['baseName']) + '(?!·))' + '[^。\\n]{0,40}(已交付|已写好|已完成|已实现|交接|handoffs?/|产出|导出|接口|结论|留言)', ptxt):
                    comms.append({'ts': d['ts'], 'kind': 'relay', 'from': other['id'], 'fromName': other['name'], 'fromAvatar': other['avatar'],
                                  'to': m['id'], 'toName': m['name'], 'toAvatar': m['avatar'],
                                  'text': f"队长把 {other['name']} 的成果/问题转达给 {m['name']}"})
                    seen_base.add(other['baseName'])
        for msg in lead_data['messages']:
            target = next((m for m in members if m['id'] == msg['to'] or msg['to'] in m['id'] or m['agentType'] == msg['to'] or m['name'] == msg['to']), None)
            comms.append({'ts': msg['ts'], 'kind': 'direct', 'from': '__lead', 'fromName': '队长', 'fromAvatar': 'team-lead',
                          'to': target['id'] if target else msg['to'], 'toName': target['name'] if target else msg['to'],
                          'toAvatar': target['avatar'] if target else 'docs-coordinator', 'text': msg['text']})
    def find_member(ref: str):
        ref = (ref or '').strip().lower()
        if not ref or ref in ('main', 'lead', '__lead', '队长'):
            return None
        for m in members:
            if ref == m['id'].lower() or ref == m['agentType'].lower() or ref == m['name'].lower() \
                    or ref == m['baseName'].lower() or ref in (m.get('dispatchName') or '').lower() and m.get('dispatchName'):
                return m
        for m in members:
            if ref in m['id'].lower() or (len(ref) > 3 and ref in m['description'].lower()):
                return m
        return None

    for m in members:
        for msg in m['messages']:
            target = find_member(msg['to'])
            if not msg.get('delivered', True):
                comms.append({'ts': msg['ts'], 'kind': 'undelivered', 'from': m['id'], 'fromName': m['name'], 'fromAvatar': m['avatar'],
                              'to': target['id'] if target else '__lead', 'toName': target['name'] if target else msg['to'],
                              'toAvatar': target['avatar'] if target else 'team-lead', 'text': f"私信 {msg['to']} 未送达：{msg['text']}"})
                continue
            if target and target['id'] != m['id']:
                comms.append({'ts': msg['ts'], 'kind': 'peer', 'from': m['id'], 'fromName': m['name'], 'fromAvatar': m['avatar'],
                              'to': target['id'], 'toName': target['name'], 'toAvatar': target['avatar'],
                              'text': ('留言 → 收件箱：' if msg.get('inbox') else '') + msg['text']})
            elif msg.get('inbox'):
                comms.append({'ts': msg['ts'], 'kind': 'peer', 'from': m['id'], 'fromName': m['name'], 'fromAvatar': m['avatar'],
                              'to': msg['to'], 'toName': msg['to'], 'toAvatar': (TEAM_CFG['agents'].get(msg['to']) or {}).get('avatar', 'docs-coordinator'),
                              'text': '留言 → 收件箱（对方尚未出场）：' + msg['text']})
            else:
                comms.append({'ts': msg['ts'], 'kind': 'report', 'from': m['id'], 'fromName': m['name'], 'fromAvatar': m['avatar'],
                              'to': '__lead', 'toName': '队长', 'toAvatar': 'team-lead', 'text': '私信队长：' + msg['text']})
    # 消息总线：本项目（或全局）的 bus 消息，从会话最早活动前 10 分钟起，画成 kind=bus 的通信
    bus_info = {'count': 0, 'unreadLead': 0, 'unreadUser': 0}
    if session_info:
        proj = session_info['project']
        all_bus, cur = _bus_scope(bus_messages(), proj), _bus_cursors()
        starts = [x for x in [m['startedAt'] for m in members] + [(lead_data or {}).get('firstTs'), chosen['mtime']] if x]
        since = min(starts) - 600
        special = {'lead': ('__lead', '队长', 'team-lead'), 'user': ('__user', '你', 'user'), 'all': ('__all', '全员', 'team-lead')}

        def bus_end(ref, ref_type=''):
            if ref in special:
                return special[ref]
            t = find_member(ref) or (find_member(ref_type) if ref_type else None)
            if t:
                return t['id'], t['name'], t['avatar']
            reg = TEAM_CFG['agents'].get(ref_type or ref) or {}
            return ref, reg.get('name') or ref, reg.get('avatar', 'docs-coordinator')
        recent = [b for b in all_bus if (b.get('ts') or 0) >= since]
        for b in recent[-200:]:
            f_id, f_name, f_av = bus_end(b.get('from', ''))
            t_id, t_name, t_av = bus_end(b.get('to', ''), b.get('toType', ''))
            comms.append({'ts': b.get('ts'), 'kind': 'bus', 'from': f_id, 'fromName': f_name, 'fromAvatar': f_av,
                          'to': t_id, 'toName': t_name, 'toAvatar': t_av, 'text': short(b.get('text', ''), 400), 'busId': b['id']})
        for m in members:
            m['unread'] = bus_unread(m['agentType'] or m['id'], proj, [m['id']], all_bus, cur)
        bus_info = {'count': len(recent), 'unreadLead': bus_unread('lead', proj, (), all_bus, cur),
                    'unreadUser': bus_unread('user', proj, (), all_bus, cur)}
    for m in members:
        m.setdefault('unread', 0)
    # 文件交接：B 读了 A 之前写过的文件（A≠B），每对 (A,B,文件) 只记一次
    writers = {}
    for m in members:
        for w in m['writes']:
            writers.setdefault(w['path'], []).append((w['ts'] or 0, m))
    seen_handoff = set()
    for m in members:
        for r in m['reads']:
            if '/.team/inbox/' in r['path']:
                continue  # 读收件箱不算交接，写收件箱已记为留言
            for wts, author in writers.get(r['path'], []):
                if author['id'] == m['id'] or (r['ts'] or 0) < wts:
                    continue
                key = (author['id'], m['id'], r['path'])
                if key in seen_handoff:
                    continue
                seen_handoff.add(key)
                comms.append({'ts': r['ts'], 'kind': 'handoff', 'from': author['id'], 'fromName': author['name'], 'fromAvatar': author['avatar'],
                              'to': m['id'], 'toName': m['name'], 'toAvatar': m['avatar'], 'text': f"交接文件 {r['label']}", 'file': r['label']})
                break

    for m in members:
        if m['status'] == 'completed' and m['endedAt']:
            comms.append({'ts': m['endedAt'], 'kind': 'report', 'from': m['id'], 'fromName': m['name'], 'fromAvatar': m['avatar'],
                          'to': '__lead', 'toName': '队长', 'toAvatar': 'team-lead', 'text': short(m['lastText'], 200) or '已完成'})
        elif m['status'] == 'error':
            comms.append({'ts': m['lastActivityAt'], 'kind': 'error', 'from': m['id'], 'fromName': m['name'], 'fromAvatar': m['avatar'],
                          'to': '__lead', 'toName': '队长', 'toAvatar': 'team-lead', 'text': '中断：' + (m.get('errorReason') or '输出达到 token 上限')})
    comms.sort(key=lambda x: -(x['ts'] or 0))

    running = sum(1 for m in members if m['status'] == 'running')
    done = sum(1 for m in members if m['status'] == 'completed')
    if not members:
        phase = 'forming'
    elif running:
        phase = 'coordinating'
    elif done == len(members):
        phase = 'finished'
    else:
        phase = 'waiting'

    feed = []
    for m in members:
        for it in m['timeline']:
            if it['kind'] == 'tool' and it['ts']:
                feed.append({'ts': it['ts'], 'memberId': m['id'], 'name': m['name'], 'avatar': m['avatar'],
                             'label': it['label'], 'error': it.get('error', False), 'done': it.get('done', False)})
    if lead_data:
        for it in lead_data['timeline']:
            if it['kind'] == 'tool' and it['ts']:
                feed.append({'ts': it['ts'], 'memberId': '__lead', 'name': '队长', 'avatar': 'team-lead',
                             'label': it['label'], 'error': it.get('error', False), 'done': it.get('done', False)})
    feed.sort(key=lambda x: -x['ts'])

    lead = {'name': 'Claude', 'avatar': 'team-lead', 'phase': phase, 'running': running, 'done': done, 'total': len(members),
            'dept': 'hq', 'model': '', 'cost': 0.0, 'usage': {}, 'pet': TEAM_CFG.get('lead_pet', '')}
    if lead_data:
        lead.update({'active': lead_data['active'], 'current': lead_data['current'], 'toolCount': lead_data['toolCount'],
                     'outputTokens': lead_data['outputTokens'], 'timeline': lead_data['timeline'],
                     'lastUserPrompt': lead_data['lastUserPrompt'], 'lastActivityAt': lead_data['mtime'],
                     'model': lead_data.get('model', ''), 'modelFamily': model_family(lead_data.get('model', '')),
                     'cost': lead_data.get('cost', 0.0), 'usage': lead_data.get('usage', {}),
                     'startedAt': lead_data.get('firstTs'), 'endedAt': lead_data.get('lastTs')})
        lu = lead.get('usage') or {}
        lctx = lu.get('in', 0) + lu.get('cache_read', 0) + lu.get('cache_write', 0)
        lead['totalTokens'] = lctx + lu.get('out', 0)
        lead['cacheHit'] = round(lu.get('cache_read', 0) / lctx, 4) if lctx else 0.0

    # 部门汇总 + 完整性
    coders = [m for m in members if m['dept'] == 'dev' or m['writes']]
    departments = []
    for d in TEAM_CFG['departments']:
        ms = [m for m in members if m['dept'] == d['id']]
        req = d['required']
        needed = req == 'always' or (req == 'code' and bool(coders)) or (req == 'code2' and len(coders) >= 2) \
            or (req == 'deliver' and len(coders) >= 2)
        if d['id'] == 'hq':
            st = 'running' if phase == 'coordinating' else 'completed' if phase == 'finished' else 'idle'
        elif any(m['status'] == 'running' for m in ms):
            st = 'running'
        elif ms and all(m['status'] == 'completed' for m in ms):
            st = 'completed'
        elif any(m['status'] in ('error', 'stalled') for m in ms):
            st = 'stalled'
        else:
            st = 'idle' if ms else 'empty'
        du = {'in': 0, 'out': 0, 'cache_read': 0, 'cache_write': 0}
        for m in ms:
            for k in du:
                du[k] += (m['usage'] or {}).get(k, 0) or 0
        dctx = du['in'] + du['cache_read'] + du['cache_write']
        dur = sum(((m['endedAt'] or m['lastActivityAt'] or 0) - (m['startedAt'] or 0)) for m in ms if m['startedAt'])
        departments.append({**d, 'members': [m['id'] for m in ms], 'status': st, 'needed': needed,
                            'missing': needed and not ms and d['id'] != 'hq' and phase == 'finished',   # 收尾后才提示“建议补位”，不算错误
                            'cost': round(sum(m['cost'] or 0 for m in ms), 4),
                            'unpriced': sum(1 for m in ms if m['cost'] is None),
                            'running': sum(1 for m in ms if m['status'] == 'running'),
                            'done': sum(1 for m in ms if m['status'] == 'completed'),
                            'failed': sum(1 for m in ms if m['status'] in ('error', 'stalled')),
                            'tools': sum(m['toolCount'] for m in ms),
                            'tokens': dctx + du['out'], 'outTokens': du['out'],
                            'cacheHit': round(du['cache_read'] / dctx, 4) if dctx else 0.0,
                            'seconds': int(dur),
                            'models': sorted({m['modelFamily'] for m in ms if m['modelFamily']})})
    model_mix = {}
    for m in members:
        fam = m['modelFamily'] or '?'
        model_mix[fam] = model_mix.get(fam, 0) + 1
    def _sum_usage(items):
        u = {'in': 0, 'out': 0, 'cache_read': 0, 'cache_write': 0}
        for it in items:
            for k in u:
                u[k] += (it or {}).get(k, 0) or 0
        u['total'] = u['in'] + u['out'] + u['cache_read'] + u['cache_write']
        ctx = u['in'] + u['cache_read'] + u['cache_write']
        u['cacheHit'] = round(u['cache_read'] / ctx, 4) if ctx else 0.0
        return u
    members_usage = _sum_usage(m['usage'] for m in members)
    lead_usage = _sum_usage([lead.get('usage')])
    all_usage = _sum_usage([members_usage, lead_usage])
    for m in members:
        u = m['usage'] or {}
        ctx = u.get('in', 0) + u.get('cache_read', 0) + u.get('cache_write', 0)
        m['totalTokens'] = ctx + u.get('out', 0)
        m['cacheHit'] = round(u.get('cache_read', 0) / ctx, 4) if ctx else 0.0
    totals = {'cost': round(sum(m['cost'] or 0 for m in members) + (lead.get('cost') or 0), 4),
              'membersCost': round(sum(m['cost'] or 0 for m in members), 4),
              'unpriced': sum(1 for m in members if m['cost'] is None) + (1 if lead.get('cost') is None and lead.get('model') else 0),
              'unpricedModels': sorted({m['model'] for m in members if m['cost'] is None and m['model']} | ({lead['model']} if lead.get('cost') is None and lead.get('model') else set())),
              'modelMix': model_mix,
              'tokens': members_usage['out'],
              'usage': members_usage, 'leadUsage': lead_usage, 'allUsage': all_usage}
    roster = [{'name': k, **{kk: vv for kk, vv in v.items() if kk != 'file'}, 'builtin': k in BUILTIN_AGENTS,
               **({'dept': TEAM_CFG['agents'][k]['dept'], 'avatar': TEAM_CFG['agents'][k]['avatar'],
                   'display': TEAM_CFG['agents'][k]['name'], 'role': TEAM_CFG['agents'][k]['role'],
                   'pet': TEAM_CFG['agents'][k].get('pet', '')} if k in TEAM_CFG['agents'] else {})}
              for k, v in agent_defs.items()]

    return {
        'pets': pet_list(), 'petInfo': pet_info(),
        'generatedAt': now, 'version': VERSION, 'update': _update_state['result'],
        'session': session_info,
        'sessions': [{'id': s['session'], 'project': s['project'], 'mtime': s['mtime'], 'count': s['count']}
                     for s in sessions[:12]],
        'lead': lead,
        'members': members,
        'departments': departments,
        'totals': totals,
        'roster': roster,
        'feed': feed[:100],
        'comms': comms[:80],
        'bus': bus_info,
        'backdrop': backdrop_state(presets=False),
    }


class Handler(SimpleHTTPRequestHandler):
    args = None
    _cache = (0.0, None)
    _lock = threading.Lock()

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        # 页面与脚本经常改，禁止浏览器缓存，避免出现“改了但没生效”的旧页面
        if self.path.startswith('/backdrops/') and not self.path.startswith('/backdrops/backdrop.json'):
            self.send_header('Cache-Control', 'public, max-age=86400')   # 背景图 url 带 ?v=<mtime>，换图自动失效
        elif not self.path.startswith('/sprites/'):
            self.send_header('Cache-Control', 'no-store, must-revalidate')
        super().end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/snapshot.json':
            q = parse_qs(u.query)
            args = argparse.Namespace(**vars(Handler.args))
            if 'session' in q:
                args.session = q['session'][0]
            elif 'project' in q or 'cwd' in q:   # msg.py who：按项目 key（或 cwd 推出的 key）取该项目最近的会话
                args.session, args.project = None, q['project'][0] if 'project' in q else resolve_project(q['cwd'][0])
            custom = 'session' in q or 'project' in q or 'cwd' in q
            with Handler._lock:
                t, data = Handler._cache
                if data is None or time.time() - t > 1.0 or custom:
                    data = json.dumps(build_snapshot(args), ensure_ascii=False)
                    if not custom:
                        Handler._cache = (time.time(), data)
            body = data.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == '/api/agents':
            reload_team_cfg()
            q = parse_qs(u.query)
            if 'name' in q:
                return self._json(read_agent(q['name'][0]) or {'error': '成员不存在'})
            agents = [a for a in (read_agent(k) for k in sorted(load_agent_defs().keys())) if a]
            return self._json({'agents': agents, 'departments': TEAM_CFG['departments'], 'models': MODELS,
                               'efforts': EFFORTS, 'colors': COLORS, 'requiredOpts': REQUIRED_OPTS,
                               'pets': (auto_discover_pets() and pet_list()) or pet_list(), 'petInfo': pet_info(), 'shippedPets': sorted(SHIPPED_PETS)})
        if u.path == '/api/pets':
            auto_discover_pets()
            return self._json({'pets': pet_list(), 'petInfo': pet_info(), 'shippedPets': sorted(SHIPPED_PETS)})
        if u.path == '/api/update/check':
            q = parse_qs(u.query)
            return self._json({'version': VERSION, **update_check(force='force' in q)})
        if u.path in ('/api/bus', '/api/bus/inbox'):
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            project = q.get('project') or resolve_project(q.get('cwd', ''))
            try:
                if u.path == '/api/bus':
                    return self._json(bus_log(project, int(q.get('limit') or 200), q.get('session', '')))
                flag = lambda k: q.get(k, '') not in ('', '0', 'false')
                return self._json(bus_inbox(q.get('to', ''), project, peek=flag('peek'), everything=flag('all')))
            except ValueError as e:
                return self._json({'error': str(e)}, 400)
        if u.path == '/api/backdrop':
            return self._json(backdrop_state())
        if u.path == '/api/wallpapers' or u.path.startswith('/api/wallpapers/'):
            return self._handle_wallpapers_get(u.path)
        if self._is_bus_file():
            return self._json({'error': 'not found'}, 404)   # 消息原文件不走静态服务
        if u.path == '/':
            self.path = '/index.html'
        return super().do_GET()

    def _handle_wallpapers_get(self, path: str):
        """GET /api/wallpapers、/api/wallpapers/desktop、/api/wallpapers/<id>/(preview|media)。
        文件只从扫描结果的白名单路径服务（wallpapers.resolve_media / desktop_wallpaper_path），杜绝任意路径读取。"""
        wp = _wp()
        if path == '/api/wallpapers':
            return self._json(wp.list_wallpapers())
        if path == '/api/wallpapers/desktop':
            p = wp.desktop_wallpaper_path()
            if not p:
                return self._json({'error': '没有可用的桌面壁纸快照'}, 404)
            return wp.serve_file(self, p)
        parts = path.split('/')   # ['', 'api', 'wallpapers', '<id>', 'preview'|'media'|'probe']
        if len(parts) == 5 and parts[4] == 'probe':
            info = wp.probe_item(parts[3])
            if info is None:
                return self._json({'error': 'not found'}, 404)
            return self._json(info)
        if len(parts) == 5 and parts[4] in ('preview', 'media'):
            p = wp.resolve_media(parts[3], parts[4])
            if not p:
                return self._json({'error': 'not found'}, 404)
            return wp.serve_file(self, p)
        return self._json({'error': 'not found'}, 404)

    def _is_bus_file(self) -> bool:
        """按真实落盘路径判断（/BUS/、/./bus/、%2F 等写法都挡住）：bus/ 下的消息原文件不走静态服务。"""
        try:
            t = Path(self.translate_path(self.path)).resolve()
            return any(t == b or b in t.parents for b in {BUS.resolve(), (HERE / 'bus').resolve()})
        except Exception:
            return True

    def do_HEAD(self):
        if self._is_bus_file():
            return self._json({'error': 'not found'}, 404)
        return super().do_HEAD()

    def _same_origin(self) -> bool:
        """没有 Origin（msg.py / curl）放行；有 Origin 必须是本机回环地址 + 本服务端口（别的网站、本机别的端口的页面都不行）。"""
        origin = self.headers.get('Origin')
        if not origin:
            return True
        try:
            o = urlparse(origin)
            port = o.port or (443 if o.scheme == 'https' else 80)
        except ValueError:
            return False
        return o.hostname in ('127.0.0.1', 'localhost', '::1') and port == self.server.server_address[1]

    def do_POST(self):
        u = urlparse(self.path)
        if not self._same_origin():
            return self._json({'error': '拒绝跨站请求'}, 403)   # 别的网页不能往本机看板 POST（防止借 bus 给代理注入消息）
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > PET_MAX_BYTES * 2:
                return self._json({'error': '请求体过大'}, 413)
            if n < 0:
                raise ValueError('bad Content-Length')
            payload = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except Exception:
            return self._json({'error': '请求体不是合法 JSON'}, 400)
        try:
            reload_team_cfg()
            if u.path == '/api/agents':
                out = write_agent(payload)
                Handler._cache = (0.0, None)
                return self._json({'ok': True, 'agent': out, 'note': '已写入 ~/.claude/agents；新开的 Claude Code 会话即可派它'})
            if u.path == '/api/agents/delete':
                delete_agent(str(payload.get('name', '')))
                Handler._cache = (0.0, None)
                return self._json({'ok': True})
            if u.path == '/api/departments':
                out = write_department(payload)
                Handler._cache = (0.0, None)
                return self._json({'ok': True, 'department': out, 'departments': TEAM_CFG['departments']})
            if u.path in ('/api/pets', '/api/pets/import'):
                out = write_pet(payload)
                Handler._cache = (0.0, None)
                return self._json({'ok': True, 'pet': out, 'pets': pet_list(), 'petInfo': pet_info()})
            if u.path == '/api/update/apply':
                return self._json(update_apply())
            if u.path == '/api/shutdown':
                self._json({'ok': True})
                threading.Timer(0.5, lambda: os._exit(0)).start()
                return
            if u.path == '/api/members/pet':
                out = set_member_pet(payload)
                Handler._cache = (0.0, None)
                return self._json({'ok': True, **out})
            if u.path == '/api/pets/fit':
                pet = str(payload.get('id', '')).strip()
                if payload.get('restore'):
                    sys.path.insert(0, str(HERE)); import fit_pet
                    f = SPRITES / pet_files().get(pet, '')
                    out = {'restored': fit_pet.restore_file(f)} if f.is_file() else {'error': '形象不存在'}
                else:
                    out = fit_pet_file(pet, fix_cropped=bool(payload.get('fixCropped')))
                Handler._cache = (0.0, None)
                return self._json({'ok': True, 'fit': out, 'petInfo': pet_info()})
            if u.path == '/api/pets/delete':
                delete_pet(str(payload.get('id', '')))
                Handler._cache = (0.0, None)
                return self._json({'ok': True, 'pets': pet_list(), 'petInfo': pet_info()})
            if u.path == '/api/bus/send':
                out = bus_send(payload)
                Handler._cache = (0.0, None)
                return self._json(out)
            if u.path == '/api/backdrop':
                try:
                    out = write_backdrop(payload)
                except BackdropFetchError as e:
                    return self._json({'error': str(e)}, 502)
                Handler._cache = (0.0, None)
                return self._json(out)
            if u.path == '/api/departments/delete':
                delete_department(str(payload.get('id', '')))
                Handler._cache = (0.0, None)
                return self._json({'ok': True, 'departments': TEAM_CFG['departments']})
        except ValueError as e:
            return self._json({'error': str(e)}, 400)
        except Exception as e:
            return self._json({'error': f'{type(e).__name__}: {e}'}, 500)
        return self._json({'error': 'not found'}, 404)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=7788)
    ap.add_argument('--session', default=None, help='会话 id 前缀；默认取最近活跃的会话')
    ap.add_argument('--project', default=None, help='项目 key 子串')
    ap.add_argument('--stale', type=float, default=600, help='多少秒无更新视为停滞')
    ap.add_argument('--dump', action='store_true', help='只打印一次快照后退出')
    args = ap.parse_args()
    Handler.args = args
    if args.dump:
        print(json.dumps(build_snapshot(args), ensure_ascii=False, indent=1))
        return
    os.environ['TEAM_BOARD_PORT'] = str(args.port)   # 更新脚本重启时用同一个端口
    threading.Thread(target=auto_discover_pets, args=(True,), daemon=True).start()   # 把文件夹里已有的形象纳入
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Agent Team Board  ->  http://127.0.0.1:{args.port}/   (Ctrl+C 退出)')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
