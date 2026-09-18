#!/usr/bin/env python3
"""
从 petdex（https://petdex.dev，社区桌宠画廊，4800+ 只，与 Codex 宠物同格式）下载形象到看板的 sprites/。

    python fetch_petdex.py boba homelander          # 按 slug 下载
    python fetch_petdex.py --search cat             # 搜索名字里含 cat 的
    python fetch_petdex.py --list --search robot    # 只列不下载
    python fetch_petdex.py --starter                # 一组挑好的默认形象（见 STARTER）
    python fetch_petdex.py --force ...              # 已存在也覆盖

版权说明：petdex 上的形象由各自作者上传，没有统一授权，本仓库**不附带**它们；这个脚本只把它们下载到
**你自己的电脑**上使用，并把作者（submittedBy）与来源链接写进 sprites/<id>.json，看板里能看到。
作者如要求下架，请到 https://petdex.dev 处理。纯标准库。
"""
import json
import sys
import urllib.request
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


def fetch(url: str, timeout=60) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def manifest():
    cache = HERE / '.petdex-manifest.json'
    try:
        data = fetch(MANIFEST)
        cache.write_bytes(data)
    except Exception as e:
        if not cache.exists():
            sys.exit(f'拿不到 petdex 清单（{e}），检查网络')
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


def install(p: dict, force=False) -> bool:
    url = p.get('zipUrl') or p.get('spritesheetUrl')
    try:
        data = fetch(url)
        meta, ext, img = tb.parse_pet_package(data, p['slug'])
        meta['id'] = tb.slugify_pet_id(p['slug'])
        meta.setdefault('displayName', p.get('displayName') or p['slug'])
        meta['spriteVersionNumber'] = p.get('spriteVersionNumber') or meta.get('spriteVersionNumber')
        info = tb.save_pet(meta, ext, img, overwrite=force,
                           source=f"https://petdex.dev/pets/{p['slug']}",
                           credit=f"petdex · {p.get('submittedBy') or '未署名'}")
        print(f"  {info['id']:20s} {len(img)/1024:6.0f} KB  {info['width']}x{info['height']}  作者 {p.get('submittedBy') or '未署名'}")
        return True
    except ValueError as e:
        if '已存在' in str(e):
            print(f"  {p['slug']:20s} 已有，跳过（加 --force 覆盖）")
        else:
            print(f"  {p['slug']:20s} 跳过：{e}")
    except Exception as e:
        print(f"  {p['slug']:20s} 下载失败：{type(e).__name__}: {e}")
    return False


def main():
    args = sys.argv[1:]
    force = '--force' in args
    only_list = '--list' in args
    search = ''
    if '--search' in args:
        i = args.index('--search')
        search = args[i + 1] if i + 1 < len(args) else ''
        del args[i:i + 2]
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
    n = sum(install(p, force) for p in chosen)
    print(f'完成：{n} 只已放入 sprites/。刷新看板即可选用（无需重启服务）。')


if __name__ == '__main__':
    main()
