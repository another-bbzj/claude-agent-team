#!/usr/bin/env python3
"""
从 petdex（https://petdex.dev，社区桌宠画廊，4800+ 只，与 Codex 宠物同格式）下载形象到看板的 sprites/。

    python fetch_petdex.py boba homelander          # 按 slug 下载
    python fetch_petdex.py --search cat             # 搜索名字里含 cat 的
    python fetch_petdex.py --list --search robot    # 只列不下载
    python fetch_petdex.py --starter                # 一组挑好的默认形象（见 STARTER）
    python fetch_petdex.py --force ...              # 已存在也覆盖
    python fetch_petdex.py --list-packs             # 列出角色包
    python fetch_petdex.py --pack games             # 下载整个角色包（已存在跳过）
    python fetch_petdex.py --pack games --assign    # 下载后按 PACKS 表分配岗位（只分配未自定义形象的岗位）
    python fetch_petdex.py --pack games --assign --assign-all   # 分配时强制覆盖已自定义的岗位

版权说明：petdex 上的形象由各自作者上传，没有统一授权，本仓库**不附带**它们；这个脚本只把它们下载到
**你自己的电脑**上使用，并把作者（submittedBy）与来源链接写进 sprites/<id>.json，看板里能看到。
作者如要求下架，请到 https://petdex.dev 处理。纯标准库。
"""
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import team_board as tb  # noqa: E402

MANIFEST = 'https://petdex.dev/api/manifest'
UA = {'User-Agent': 'claude-agent-team/1.0 (+https://github.com/another-bbzj/claude-agent-team)'}
# 默认一组：偏“生物 / 物件”类原创形象，尽量避开真人与知名角色
STARTER = ['boba', 'milkfrog', 'cactus', 'green-slime', 'ghost', 'capy-pixel', 'whaley', 'panda-default', 'working-mushrooms',
           'dino-box', 'cloudy', 'terminal-ghost-2', 'glitchcat', 'byte-bunny', 'stout-corgi', 'peri-the-owl']

# 角色包：SPEC §9（v1.5.0 游戏角色包）。role 是一键分配的默认岗位（subagent_type，'lead' 特指队长），
# None 表示不参与自动分配（只下载，用户自己挑）。name 是看板里显示的中文名，覆盖 petdex 自己的 displayName。
PACKS = {
    'games': [
        {'slug': 'amiya', 'name': '阿米娅', 'franchise': '明日方舟', 'role': 'lead'},
        {'slug': 'furina-2', 'name': '芙宁娜', 'franchise': '原神', 'role': 'team-architect'},
        {'slug': 'firefly-2', 'name': '流萤', 'franchise': '崩坏：星穹铁道', 'role': 'backend-dev'},
        {'slug': 'march7th', 'name': '三月七', 'franchise': '崩坏：星穹铁道', 'role': 'frontend-dev'},
        {'slug': 'nahida-2', 'name': '纳西妲', 'franchise': '原神', 'role': 'qa-tester'},
        {'slug': 'star-rail', 'name': '黄泉', 'franchise': '崩坏：星穹铁道', 'role': 'code-reviewer'},
        {'slug': 'paimon', 'name': '派蒙', 'franchise': '原神', 'role': 'researcher'},
        {'slug': 'genshin-impact-keqing', 'name': '刻晴', 'franchise': '原神', 'role': 'docs-writer'},
        {'slug': 'jinx', 'name': '金克丝', 'franchise': '英雄联盟', 'role': 'release-ops'},
        {'slug': 'exusiai-seekers-song', 'name': '能天使·寻翼之歌', 'franchise': '明日方舟', 'role': None},
        {'slug': 'hutao-2', 'name': '胡桃', 'franchise': '原神', 'role': None},
        {'slug': 'zelda-botw', 'name': '塞尔达', 'franchise': '塞尔达传说', 'role': None},
        {'slug': 'skybound-hero', 'name': '林克', 'franchise': '塞尔达传说', 'role': None},
        {'slug': 'xiao-qishi', 'name': '小骑士', 'franchise': '空洞骑士', 'role': None},
        {'slug': 'kirby', 'name': '卡比', 'franchise': '星之卡比', 'role': None},
        {'slug': 'hatsune-miku-2', 'name': '初音未来', 'franchise': 'VOCALOID', 'role': None},
        {'slug': 'ahri-spirit-blossom', 'name': '灵魂莲华·阿狸', 'franchise': '英雄联盟', 'role': None},
        {'slug': 'capvolt', 'name': '皮卡丘', 'franchise': '宝可梦', 'role': None},
        {'slug': 'minecraft-steve', 'name': '史蒂夫', 'franchise': '我的世界', 'role': None},
        {'slug': 'amamiya-ren-phantom', 'name': '雨宫莲', 'franchise': '女神异闻录 5', 'role': None},
    ],
}
PACK_NAMES = {'games': '游戏角色包'}


def fetch(url: str, timeout=60) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def manifest(strict: bool = True):
    """strict=True（命令行直接用）：拿不到清单又没缓存就退出进程；strict=False（供 team_board.py 当库调用）：
    改成抛 RuntimeError，让调用方转成 502 返回前端，别把整个 HTTP 服务进程带崩。"""
    cache = HERE / '.petdex-manifest.json'
    try:
        data = fetch(MANIFEST)
        cache.write_bytes(data)
    except Exception as e:
        if not cache.exists():
            msg = f'拿不到 petdex 清单（{e}），检查网络'
            if strict:
                sys.exit(msg)
            raise RuntimeError(msg) from e
        print(f'联网失败（{e}），用上次缓存的清单')
        data = cache.read_bytes()
    return json.loads(data.decode('utf-8'))['pets']


def pick(pets, slugs=(), search=''):
    by = {p['slug']: p for p in pets}
    out = []
    for s in slugs:
        s = s.strip().lower()
        if s in by:
            out.append(by[s])
        else:  # 没有精确 slug 时按前缀 / 名字找第一个
            cand = [p for p in pets if p['slug'].startswith(s) or s in (p.get('displayName') or '').lower()]
            if cand:
                out.append(cand[0])
            else:
                print('找不到：', s)
    if search:
        q = search.lower()
        out += [p for p in pets if q in p['slug'] or q in (p.get('displayName') or '').lower()]
    seen, uniq = set(), []
    for p in out:
        if p['slug'] not in seen:
            seen.add(p['slug'])
            uniq.append(p)
    return uniq


def install(p: dict, force=False, override: dict = None):
    """下载单只到 sprites/。override 可覆盖 displayName / franchise（角色包用中文名，不用 petdex 自己的英文名）。
    返回 (ok, err)：ok=True 表示已在 sprites/ 里（含"已存在跳过"）；ok=False 时 err 是给用户看的中文原因。"""
    override = override or {}
    url = p.get('zipUrl') or p.get('spritesheetUrl')
    try:
        data = fetch(url)
        meta, ext, img = tb.parse_pet_package(data, p['slug'])
        meta['id'] = tb.slugify_pet_id(p['slug'])
        meta.setdefault('displayName', p.get('displayName') or p['slug'])
        if override.get('displayName'):
            meta['displayName'] = override['displayName']
        if override.get('franchise'):
            meta['franchise'] = override['franchise']
        meta['spriteVersionNumber'] = p.get('spriteVersionNumber') or meta.get('spriteVersionNumber')
        info = tb.save_pet(meta, ext, img, overwrite=force,
                           source=f"https://petdex.dev/pets/{p['slug']}",
                           credit=f"petdex · {p.get('submittedBy') or '未署名'}",
                           franchise=override.get('franchise') or '')
        print(f"  {info['id']:20s} {len(img)/1024:6.0f} KB  {info['width']}x{info['height']}  作者 {p.get('submittedBy') or '未署名'}")
        return True, None
    except ValueError as e:
        if '已存在' in str(e):
            print(f"  {p['slug']:20s} 已有，跳过（加 --force 覆盖）")
            return True, None
        print(f"  {p['slug']:20s} 跳过：{e}")
        return False, str(e)
    except Exception as e:
        err = f'{type(e).__name__}: {e}'
        print(f"  {p['slug']:20s} 下载失败：{err}")
        return False, err


def download_pack_items(pets: list, items: list, force: bool = False):
    """并发（≤4）下载角色包（PACKS['games'] 那种列表）里的每一项；单个失败不影响其他。
    返回 (installed_slugs, failed[{slug,error}])。"""
    by = {p['slug']: p for p in pets}

    def one(it):
        p = by.get(it['slug'])
        if not p:
            return it['slug'], False, '在 petdex 清单里找不到这个 slug（可能已下架）'
        ok, err = install(p, force=force, override={'displayName': it['name'], 'franchise': it['franchise']})
        return it['slug'], ok, err

    installed, failed = [], []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for slug, ok, err in ex.map(one, items):
            if ok:
                installed.append(slug)
            else:
                failed.append({'slug': slug, 'error': err})
    return installed, failed


def main():
    args = sys.argv[1:]
    force = '--force' in args
    only_list = '--list' in args
    list_packs = '--list-packs' in args
    assign = '--assign' in args
    assign_all = '--assign-all' in args
    pack_id = None
    if '--pack' in args:
        i = args.index('--pack')
        pack_id = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    search = ''
    if '--search' in args:
        i = args.index('--search')
        search = args[i + 1] if i + 1 < len(args) else ''
        del args[i:i + 2]

    if list_packs:
        for pid, items in PACKS.items():
            print(f"  {pid:10s} {PACK_NAMES.get(pid, pid):12s} {len(items)} 只")
        return

    if pack_id:
        items = PACKS.get(pack_id)
        if not items:
            sys.exit(f'没有这个角色包：{pack_id}（--list-packs 查看）')
        print(f'下载角色包「{PACK_NAMES.get(pack_id, pack_id)}」（{len(items)} 只）到 {HERE / "sprites"}，并发 ≤4')
        pets = manifest()
        installed, failed = download_pack_items(pets, items, force=force)
        print(f'完成：{len(installed)} 成功，{len(failed)} 失败')
        for f in failed:
            print(f"  失败: {f['slug']}  {f['error']}")
        if assign or assign_all:
            assigned = tb.assign_pack_roles(pack_id, force_all=assign_all)
            print('分配：' + (', '.join(f'{k}={v}' for k, v in assigned.items()) if assigned else '（无，岗位都已有自定义形象；--assign-all 强制覆盖）'))
        return

    slugs = [a for a in args if not a.startswith('--')]
    if '--starter' in args:
        slugs = STARTER + slugs
    if not slugs and not search:
        print(__doc__)
        return
    pets = manifest()
    chosen = pick(pets, slugs, search)
    if not chosen:
        sys.exit('没有匹配的形象')
    if only_list:
        for p in chosen[:200]:
            print(f"  {p['slug']:36s} {p.get('displayName') or '':30s} v{p.get('spriteVersionNumber', 1)}  {p.get('kind', '')}  by {p.get('submittedBy') or '?'}")
        print(f'共 {len(chosen)} 只')
        return
    if len(chosen) > 40 and not slugs:
        print(f'搜索结果 {len(chosen)} 只，太多了；先用 --list 看看，再按 slug 指定')
        return
    print(f'下载 {len(chosen)} 只到 {HERE / "sprites"}')
    n = sum(1 for ok, _ in (install(p, force) for p in chosen) if ok)
    print(f'完成：{n} 只已放入 sprites/。刷新看板即可选用（无需重启服务）。')


if __name__ == '__main__':
    main()
