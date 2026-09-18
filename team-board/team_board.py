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
                except Exception:
                    pass
            out[f.stem] = entry
    return out


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
    return {'id': pet, 'file': f'{pet}.{ext}', 'width': w, 'height': h, 'rows': rows, 'displayName': side['displayName']}


def write_pet(d: dict) -> dict:
    """HTTP 导入：{id?, data(base64 或 dataURL), filename?, overwrite?, source?, credit?}。data 可以是雪碧图或 zip 包。"""
    import base64
    raw = str(d.get('data', ''))
    if raw.lstrip().startswith('data:') and ',' in raw[:200]:
        raw = raw.split(',', 1)[1]
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:
        raise ValueError('文件数据不是合法的 base64')
    if not data or len(data) > PET_MAX_BYTES:
        raise ValueError(f'文件为空或超过 {PET_MAX_BYTES // 1024 // 1024} MB')
    hint = str(d.get('filename') or '').rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
    hint = re.sub(r'\.(zip|webp|png)$', '', hint, flags=re.I)
    hint = re.sub(r'[-_ ]?sprite(sheet)?.*$', '', hint, flags=re.I) or hint
    meta, ext, img = parse_pet_package(data, hint)
    if str(d.get('id', '')).strip():
        meta['id'] = slugify_pet_id(d['id'])
    return save_pet(meta, ext, img, overwrite=bool(d.get('overwrite')), source=str(d.get('source', '')), credit=str(d.get('credit', '')))


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
    (SPRITES / files[pet]).unlink()
    if pet_meta_path(pet).exists():
        pet_meta_path(pet).unlink()



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
    if model not in MODELS and not re.match(r'^[a-z0-9.-]+$', model):
        raise ValueError('模型只能是 sonnet / opus / haiku / fable / inherit 或完整模型 id')
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
    # 第三方模型（deepseek / gpt / qwen …）：取 id 的首段作为家族名，方便按家族汇总
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


def real_model(model) -> bool:
    """Claude Code 本地合成的消息（中断、错误回执等）model 写成 <synthetic>，不是真的模型调用，
    不能拿来覆盖成员的模型归属，否则会显示成“未定价”。"""
    return bool(model) and not str(model).startswith('<')


class UsageLedger:
    """按 message.id 去重累计 usage：同一条 API 消息会被写成多行（每个 content block 一行，usage 是当时的快照），
    只取每条消息的最后一份快照，否则 token 会被重复计 2-5 倍。"""

    def __init__(self):
        self.by_id = {}
        self.order = []
        self.anon = 0

    def add(self, msg: dict):
        usage = msg.get('usage') or {}
        if not usage:
            return
        mid = msg.get('id')
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
            ledger.add(msg)
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
        status = 'error'
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
        notifications = {}
        lead_model, lead_ledger = '', UsageLedger()
        for e in read_jsonl(path):
            if e.get('isSidechain'):
                continue
            ts = parse_iso(e.get('timestamp'))
            if ts:
                last_ts = ts
                first_ts = first_ts or ts
            et = e.get('type')
            if et in ('queue-operation', 'attachment'):
                raw = json.dumps(e, ensure_ascii=False)
                if 'task-notification' in raw:
                    tid = re.search(r'<task-id>(\w+)</task-id>', raw)
                    nst = re.search(r'<status>(\w+)</status>', raw)
                    if tid and nst:
                        notifications[tid.group(1)] = {'status': nst.group(1), 'ts': ts}
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
                lead_ledger.add(msg)
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
                'lastTs': last_ts, 'firstTs': first_ts, 'mtime': st.st_mtime}
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


def build_snapshot(args):
    reload_team_cfg()
    now = time.time()
    sessions = discover_sessions()
    if args.session:
        chosen = next((s for s in sessions if s['session'].startswith(args.session)), None)
    elif args.project:
        chosen = next((s for s in sessions if args.project in s['project']), None)
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
    else:
        lead_data = None
    members = [m for m in members if m]

    # 部门归属 + agent 定义（model/effort 来自 ~/.claude/agents/*.md）
    agent_defs = load_agent_defs()
    for m in members:
        cfg = TEAM_CFG['agents'].get(m['agentType'])
        d = agent_defs.get(m['agentType'], {})
        m['dept'] = cfg['dept'] if cfg else TEAM_CFG['avatar_dept'].get(m['avatar'], 'dev')
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
                          'to': '__lead', 'toName': '队长', 'toAvatar': 'team-lead', 'text': '中断：输出达到 token 上限'})
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
            'dept': 'hq', 'model': '', 'cost': 0.0, 'usage': {}}
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
        needed = req == 'always' or (req == 'code' and bool(members)) or (req == 'code2' and len(coders) >= 2) \
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
                            'missing': needed and not ms and d['id'] != 'hq',
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
        'generatedAt': now,
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
        if not self.path.startswith('/sprites/'):
            self.send_header('Cache-Control', 'no-store, must-revalidate')
        super().end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/snapshot.json':
            q = parse_qs(u.query)
            args = argparse.Namespace(**vars(Handler.args))
            if 'session' in q:
                args.session = q['session'][0]
            with Handler._lock:
                t, data = Handler._cache
                if data is None or time.time() - t > 1.0 or 'session' in q:
                    data = json.dumps(build_snapshot(args), ensure_ascii=False)
                    if 'session' not in q:
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
                               'pets': pet_list(), 'petInfo': pet_info(), 'shippedPets': sorted(SHIPPED_PETS)})
        if u.path == '/api/pets':
            return self._json({'pets': pet_list(), 'petInfo': pet_info(), 'shippedPets': sorted(SHIPPED_PETS)})
        if u.path == '/':
            self.path = '/index.html'
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n > PET_MAX_BYTES * 2:
                return self._json({'error': '请求体过大'}, 413)
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
            if u.path == '/api/pets':
                out = write_pet(payload)
                Handler._cache = (0.0, None)
                return self._json({'ok': True, 'pet': out, 'pets': pet_list(), 'petInfo': pet_info()})
            if u.path == '/api/pets/delete':
                delete_pet(str(payload.get('id', '')))
                Handler._cache = (0.0, None)
                return self._json({'ok': True, 'pets': pet_list(), 'petInfo': pet_info()})
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
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Agent Team Board  ->  http://127.0.0.1:{args.port}/   (Ctrl+C 退出)')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
