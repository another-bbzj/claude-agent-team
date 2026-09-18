#!/usr/bin/env python3
"""
识别雪碧图里的角色主体，把偏小 / 各动作大小不一的形象放大到和 Codex 自带角色一样高。

    python fit_pet.py <形象id 或 图片路径> [--dry] [--fix-cropped]
    python fit_pet.py --all              # 处理 sprites/ 里所有非仓库自带的形象
    python fit_pet.py --restore <id>     # 用 <id>.orig.* 还原

做法（每一行 = 一个动作，8 列 = 帧）：
  1. 逐格找不透明像素的包围盒（alpha > 24，一行至少 6 个像素才算，忽略头顶光点之类的碎屑）；
  2. 以“站得最高的那一行”为基准高度（至少取格高的 82%），其余每行按 基准 / 本行高度 算倍率，
     同一行所有帧共用一个倍率——动作内部稳定，动作之间不再忽大忽小；
  3. 主体贴着格子底边的行（身体被生成器裁掉了，比如只剩个大脑袋）不放大，否则裁口更明显；
     加 --fix-cropped 会用 idle 行的帧顶替这些被裁的行；
  4. 格子里放得下的部分直接改图（最近邻放大保留像素风，脚底基线不动，原图另存为 <id>.orig.<ext>）；
     放不下的部分（翅膀太宽等）写进 <id>.json 的 fitRows，由看板在屏幕上按行放大显示（不会裁切）。
需要 Pillow（pip install pillow）；没装时只做分析并写 fitRows，同样能让看板放大显示。
"""
import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
SPRITES = HERE / 'sprites'
COLS, CW, CH = 8, 192, 208
ALPHA_MIN, ROW_MIN_PX = 24, 6
TARGET, KMAX, KROW = 0.82, 3.0, 1.35   # KMAX：总倍率上限（图内部分受格子限制）；KROW：行间归一最多 1.35，避免把刻意画小的动作拉太大
KSCREEN = 1.6                          # 屏幕上（CSS）最多再放大 1.6，翅膀再宽也不会撑出人物栏


def _pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def body_box(alpha, x0, y0, w, h):
    """格子 (x0,y0,w,h) 内主体包围盒 (l,t,r,b, mass)（含端点；mass = 不透明像素数）；空格返回 None。"""
    top = bottom = None
    left, right = w, -1
    mass = 0
    for y in range(h):
        first = last = -1; n = 0
        for x in range(w):
            if alpha[x0 + x, y0 + y] > ALPHA_MIN:
                n += 1
                if first < 0:
                    first = x
                last = x
        mass += n
        if n >= ROW_MIN_PX:
            if top is None:
                top = y
            bottom = y
            left = min(left, first); right = max(right, last)
    if top is None:
        return None
    return left, top, right, bottom, mass


def analyze(img, cw=CW, ch=CH):
    """逐行统计：rows[r] = {h, w, cropped, boxes:[(col, box)]}；standing = 基准高度。"""
    img = img.convert('RGBA')
    a = img.getchannel('A').load()
    cols, nrows = img.width // cw, img.height // ch
    rows = []
    for r in range(nrows):
        boxes, h, w, mass, cropped = [], 0, 0, 0, False
        for c in range(cols):
            box = body_box(a, c * cw, r * ch, cw, ch)
            boxes.append((c, box))
            if box:
                h = max(h, box[3] - box[1] + 1); w = max(w, box[2] - box[0] + 1); mass = max(mass, box[4])
                if box[3] >= ch - 1:
                    cropped = True
        rows.append({'h': h, 'w': w, 'mass': mass, 'cropped': cropped, 'boxes': boxes})
    return finish_rows(rows, cols, ch)


def finish_rows(rows, cols, ch):
    """由各行 {h, w, mass, cropped} 算倍率（与看板 index.html 里的 canvas 版保持同一算法）：
    · 行间用“像素质量”归一（sqrt(最大质量 / 本行质量)）——同一角色换姿势质量不变，趴下 / 蹲下不会被硬拉高；
    · 整体偏小（最壮那一行的高度 < 格高 82%）再乘一个全局倍率；
    · 上限 KMAX，屏幕上不超过格高 1.6 倍；差异 < 8% 视为相同，避免动作切换时忽大忽小；
    · 被裁的行只用全局倍率。"""
    full = [r for r in rows if r['mass']]
    if not full:
        for r in rows:
            r['k'] = 1.0
        return {'cols': cols, 'rows': rows, 'standing': 0, 'target': 0}
    ref = max(full, key=lambda r: r['mass'])
    standing = ref['h']
    target = max(standing, TARGET * ch)
    glob = max(1.0, target / standing) if standing else 1.0
    for r in rows:
        if not r['mass']:
            r['k'] = 1.0; continue
        k = glob if r['cropped'] else min(KROW, (ref['mass'] / r['mass']) ** 0.5) * glob
        k = min(k, KMAX)
        r['k'] = 1.0 if k < 1.08 else round(k, 3)
    return {'cols': cols, 'rows': rows, 'standing': standing, 'target': round(target, 1)}


def plan(info, cw=CW, ch=CH):
    """把每行倍率拆成 图内可放大部分 k_in（受格宽 / 格高限制）与 剩余 k_out（交给看板显示）。"""
    out = []
    for r in info['rows']:
        k = r['k']
        if k <= 1.0 or not r['h']:
            out.append((1.0, min(KSCREEN, k) if k > 1.0 else 1.0)); continue
        k_in = min(k, (cw - 2) / max(r['w'], 1), (ch - 1) / max(r['h'], 1))
        k_in = 1.0 if k_in < 1.03 else round(k_in, 3)
        k_out = min(KSCREEN, round(k / k_in, 3) if k_in > 1.0 else k)
        out.append((k_in, 1.0 if k_out < 1.02 else k_out))
    return out


def rebuild(img, info, plan_rows, fix_cropped=False, cw=CW, ch=CH):
    Image = _pil()
    img = img.convert('RGBA')
    out = Image.new('RGBA', img.size, (0, 0, 0, 0))
    idle = info['rows'][0]
    for r, row in enumerate(info['rows']):
        src_row, k_in = row, plan_rows[r][0]
        if fix_cropped and row['cropped'] and idle['h'] and not idle['cropped']:
            src_row, k_in = idle, plan_rows[0][0]
            src_r = 0
        else:
            src_r = r
        for c, box in src_row['boxes']:
            if not box:
                continue
            l, t, rr, b = box[:4]
            crop = img.crop((c * cw + l, src_r * ch + t, c * cw + rr + 1, src_r * ch + b + 1))
            if k_in > 1.0:
                crop = crop.resize((max(1, round(crop.width * k_in)), max(1, round(crop.height * k_in))), Image.NEAREST)
            foot_gap = ch - 1 - b
            x = int(round((l + rr + 1) / 2 - crop.width / 2)); y = ch - foot_gap - crop.height
            x = max(0, min(cw - crop.width, x)); y = max(0, min(ch - crop.height, y))
            out.alpha_composite(crop, (c * cw + x, r * ch + y))
    return out


def sidecar_path(path: Path) -> Path:
    return path.with_name(path.stem + '.json')


def write_sidecar(path: Path, fit_rows, info):
    sp = sidecar_path(path)
    meta = {}
    if sp.exists():
        try:
            meta = json.loads(sp.read_text(encoding='utf-8'))
        except Exception:
            meta = {}
    if any(k > 1.0 for k in fit_rows):
        meta['fitRows'] = fit_rows
    else:
        meta.pop('fitRows', None)
    meta['bodyRows'] = [{'h': r['h'], 'w': r['w'], 'mass': r['mass'], 'cropped': r['cropped']} for r in info['rows']]
    meta['standing'] = info['standing']
    sp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')


def fit_file(path: Path, dry=False, fix_cropped=False, backup=True) -> dict:
    Image = _pil()
    if Image is None:
        raise RuntimeError('需要 Pillow：pip install pillow')
    img = Image.open(path)
    cw = img.width // COLS
    ch = int(round(cw * CH / CW))
    info = analyze(img, cw, ch)
    pl = plan(info, cw, ch)
    res = {'file': path.name, 'rows': len(info['rows']), 'standing': info['standing'], 'target': info['target'],
           'rowScale': [r['k'] for r in info['rows']], 'inImage': [p[0] for p in pl], 'onScreen': [p[1] for p in pl],
           'cropped': [i for i, r in enumerate(info['rows']) if r['cropped']], 'changed': False}
    if dry:
        return res
    need_img = any(p[0] > 1.0 for p in pl) or (fix_cropped and res['cropped'])
    if need_img:
        if backup:
            orig = path.with_name(f'{path.stem}.orig{path.suffix}')
            if not orig.exists():
                orig.write_bytes(path.read_bytes())
        out = rebuild(img, info, pl, fix_cropped, cw, ch)
        if path.suffix.lower() == '.webp':
            out.save(path, 'WEBP', lossless=True, quality=100, method=4)
        else:
            out.save(path, 'PNG', optimize=True)
        res['changed'] = True
    write_sidecar(path, [p[1] for p in pl], info)
    return res


def restore_file(path: Path) -> bool:
    orig = path.with_name(f'{path.stem}.orig{path.suffix}')
    sp = sidecar_path(path)
    if sp.exists():
        try:
            meta = json.loads(sp.read_text(encoding='utf-8'))
            for k in ('fitRows', 'bodyRows', 'standing'):
                meta.pop(k, None)
            sp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass
    if not orig.exists():
        return False
    path.write_bytes(orig.read_bytes()); orig.unlink()
    return True


def resolve(arg: str) -> Path:
    p = Path(arg)
    if p.exists():
        return p
    for ext in ('webp', 'png'):
        q = SPRITES / f'{arg}.{ext}'
        if q.exists():
            return q
    sys.exit(f'找不到形象或文件：{arg}')


def main():
    args = sys.argv[1:]
    if not args or '-h' in args or '--help' in args:
        print(__doc__); return
    names = [a for a in args if not a.startswith('--')]
    if '--restore' in args:
        for n in names:
            print(('已还原 ' if restore_file(resolve(n)) else '没有备份可还原 ') + n)
        return
    if '--all' in args:
        shipped = set()
        try:
            sys.path.insert(0, str(HERE))
            import team_board
            shipped = set(team_board.SHIPPED_PETS)
        except Exception:
            pass
        files = [f for f in sorted(SPRITES.glob('*.webp')) + sorted(SPRITES.glob('*.png')) if '.orig' not in f.name and f.stem not in shipped]
    else:
        files = [resolve(a) for a in names]
    if not files:
        sys.exit('没有要处理的文件')
    for f in files:
        r = fit_file(f, dry='--dry' in args, fix_cropped='--fix-cropped' in args)
        rs = ' '.join(f'{k:.2f}' for k in r['rowScale'])
        print(f"  {f.stem:16s} 基准 {r['standing']}px  各行倍率 [{rs}]  被裁行 {r['cropped'] or '无'}  "
              f"{'已改图' if r['changed'] else ('试运行' if '--dry' in args else '图不用改')}"
              f"{'，屏幕放大 ' + ' '.join(f'{k:.2f}' for k in r['onScreen']) if any(k > 1 for k in r['onScreen']) else ''}")


if __name__ == '__main__':
    main()
