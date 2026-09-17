#!/usr/bin/env python3
"""
生成一套开源可分发的成员形象（9 只），输出到 sprites/<name>.webp。
规格与看板一致：8 列 × 11 行、每格 192×208、行序 idle / 跑右 / 跑左 / 挥手 / 跳 / 失败 / 等待 / 工作 / 审阅。

    python make_pets.py            # 生成 9 只到 ./sprites
    python make_pets.py --out DIR  # 指定输出目录

只依赖 Pillow（pip install pillow）。素材由本脚本程序化绘制，随仓库 MIT 发布。
"""
import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

CW, CH, COLS, ROWS = 192, 208, 8, 11
S = 4  # 超采样倍率
W, H = CW * S, CH * S

# 角色定义：形状 / 主色 / 亮色 / 暗色 / 特征
CHARS = {
    'pip':    dict(shape='round',   base=(93, 202, 165),  light=(159, 225, 203), dark=(29, 158, 117),  feat='antenna'),
    'cubo':   dict(shape='square',  base=(239, 159, 39),  light=(250, 199, 117), dark=(186, 117, 23),  feat='visor'),
    'drip':   dict(shape='drop',    base=(55, 138, 221),  light=(133, 183, 235), dark=(24, 95, 165),   feat='none'),
    'mush':   dict(shape='round',   base=(245, 228, 200), light=(252, 244, 230), dark=(200, 170, 120), feat='cap'),
    'kit':    dict(shape='round',   base=(180, 178, 169), light=(211, 209, 199), dark=(95, 94, 90),    feat='ears'),
    'spark':  dict(shape='round',   base=(250, 199, 117), light=(253, 232, 190), dark=(186, 117, 23),  feat='bolt'),
    'bolt':   dict(shape='capsule', base=(127, 119, 221), light=(175, 169, 236), dark=(83, 74, 183),   feat='headset'),
    'puff':   dict(shape='cloud',   base=(237, 147, 177), light=(244, 192, 209), dark=(153, 53, 86),   feat='none'),
    'tank':   dict(shape='wide',    base=(99, 153, 34),   light=(151, 196, 89),  dark=(59, 109, 17),   feat='treads'),
}

# 动画行：名字, 帧数
ANIM = [('idle', 6), ('running-right', 8), ('running-left', 8), ('waving', 4), ('jumping', 5),
        ('failed', 8), ('waiting', 6), ('running', 6), ('review', 6)]


def body_mask(shape, w, h):
    """返回 (L 模式) 身体轮廓遮罩，画在 (w,h) 画布上，底部留脚。"""
    m = Image.new('L', (w, h), 0)
    d = ImageDraw.Draw(m)
    cx, top, bottom = w // 2, int(h * 0.16), int(h * 0.86)
    if shape == 'round':
        r = int(w * 0.34); d.ellipse((cx - r, top, cx + r, top + 2 * r), fill=255)
        d.ellipse((cx - int(r * .78), top + r, cx + int(r * .78), bottom), fill=255)
    elif shape == 'square':
        d.rounded_rectangle((cx - int(w * .33), top, cx + int(w * .33), bottom), radius=int(w * .1), fill=255)
    elif shape == 'drop':
        r = int(w * 0.33)
        d.ellipse((cx - r, bottom - 2 * r, cx + r, bottom), fill=255)
        d.polygon([(cx, top - int(h * .04)), (cx - int(r * .95), bottom - r - int(r * .2)), (cx + int(r * .95), bottom - r - int(r * .2))], fill=255)
    elif shape == 'capsule':
        d.rounded_rectangle((cx - int(w * .24), top - int(h * .04), cx + int(w * .24), bottom), radius=int(w * .24), fill=255)
    elif shape == 'cloud':
        r = int(w * .2)
        for dx, dy, rr in [(-.22, .0, .9), (.22, .0, .9), (0, -.12, 1.05), (-.1, .18, .95), (.12, .18, .95)]:
            d.ellipse((cx + dx * w - r * rr, int(h * .5) + dy * h - r * rr, cx + dx * w + r * rr, int(h * .5) + dy * h + r * rr), fill=255)
    elif shape == 'wide':
        d.rounded_rectangle((cx - int(w * .42), top + int(h * .1), cx + int(w * .42), bottom), radius=int(w * .16), fill=255)
    return m


def draw_char(name, pose):
    """pose: dict(dy, sx, sy, tilt, eyes('open'|'closed'|'x'|'happy'), hand(None|'up'|'wave1'|'wave2'), prop(None|'glass'|'keys'|'sweat'|'zz'), lie(bool))"""
    c = CHARS[name]
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    # 身体
    mask = body_mask(c['shape'], W, H)
    body = Image.new('RGBA', (W, H), c['base'] + (255,))
    # 亮部：左上斜向高光；暗部：底部
    shade = Image.new('RGBA', (W, H), (0, 0, 0, 0)); sd = ImageDraw.Draw(shade)
    sd.ellipse((W * .18, H * .12, W * .62, H * .55), fill=c['light'] + (110,))
    sd.rectangle((0, H * .7, W, H), fill=c['dark'] + (70,))
    body.alpha_composite(shade)
    body.putalpha(mask)
    layer.alpha_composite(body)
    # 轮廓
    outline = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(outline)
    od.bitmap((0, 0), mask, fill=(0, 0, 0, 0))
    edge = mask.filter(__import__('PIL.ImageFilter', fromlist=['MaxFilter']).MaxFilter(9))
    ring = Image.new('RGBA', (W, H), c['dark'] + (255,)); ring.putalpha(edge)
    base_layer = Image.new('RGBA', (W, H), (0, 0, 0, 0)); base_layer.alpha_composite(ring); base_layer.alpha_composite(layer)
    layer = base_layer; ld = ImageDraw.Draw(layer)
    cx, cy = W // 2, int(H * 0.46)
    # 特征
    f = c['feat']
    if f == 'antenna':
        ld.line((cx, H * .17, cx + W * .06, H * .04), fill=c['dark'] + (255,), width=int(W * .025))
        ld.ellipse((cx + W * .06 - W * .05, H * .04 - W * .05, cx + W * .06 + W * .05, H * .04 + W * .05), fill=(232, 130, 92, 255))
    elif f == 'visor':
        ld.rounded_rectangle((cx - W * .26, cy - H * .1, cx + W * .26, cy + H * .04), radius=int(W * .05), fill=(40, 38, 46, 255))
    elif f == 'cap':
        ld.ellipse((cx - W * .42, H * .02, cx + W * .42, H * .42), fill=(216, 90, 48, 255))
        for dx, dy in [(-.2, .12), (.12, .08), (-.02, .26), (.28, .24)]:
            ld.ellipse((cx + dx * W - W * .05, H * (.1 + dy) - W * .05, cx + dx * W + W * .05, H * (.1 + dy) + W * .05), fill=(250, 236, 220, 255))
    elif f == 'ears':
        for sgn in (-1, 1):
            ld.polygon([(cx + sgn * W * .12, H * .2), (cx + sgn * W * .3, H * .0), (cx + sgn * W * .33, H * .24)], fill=c['base'] + (255,), outline=c['dark'] + (255,))
    elif f == 'bolt':
        ld.polygon([(cx + W * .02, H * -.02), (cx - W * .08, H * .12), (cx + W * .01, H * .12), (cx - W * .05, H * .22), (cx + W * .1, H * .08), (cx + W * .01, H * .08)], fill=(255, 214, 70, 255), outline=(186, 117, 23, 255))
    elif f == 'headset':
        ld.arc((cx - W * .3, H * .08, cx + W * .3, H * .5), 200, 340, fill=(40, 38, 46, 255), width=int(W * .03))
        for sgn in (-1, 1):
            ld.rounded_rectangle((cx + sgn * W * .3 - W * .06, H * .24, cx + sgn * W * .3 + W * .06, H * .4), radius=int(W * .03), fill=(40, 38, 46, 255))
    elif f == 'treads':
        for sgn in (-1, 1):
            ld.ellipse((cx + sgn * W * .26 - W * .09, H * .78, cx + sgn * W * .26 + W * .09, H * .96), fill=(60, 60, 58, 255))
            ld.ellipse((cx + sgn * W * .26 - W * .04, H * .83, cx + sgn * W * .26 + W * .04, H * .91), fill=(150, 150, 146, 255))
    # 脸
    ex, ey, er = W * .13, cy - H * .02, W * .045
    eyes = pose.get('eyes', 'open')
    for sgn in (-1, 1):
        x = cx + sgn * ex
        if eyes == 'open':
            ld.ellipse((x - er, ey - er * 1.2, x + er, ey + er * 1.2), fill=(40, 38, 46, 255))
            ld.ellipse((x - er * .35 + er * .3, ey - er * .8, x + er * .35 + er * .3, ey - er * .1), fill=(255, 255, 255, 255))
        elif eyes == 'closed':
            ld.arc((x - er * 1.2, ey - er, x + er * 1.2, ey + er), 0, 180, fill=(40, 38, 46, 255), width=int(W * .018))
        elif eyes == 'happy':
            ld.arc((x - er * 1.2, ey - er * .3, x + er * 1.2, ey + er * 1.6), 180, 360, fill=(40, 38, 46, 255), width=int(W * .02))
        elif eyes == 'x':
            k = er * .9; wd = int(W * .018)
            ld.line((x - k, ey - k, x + k, ey + k), fill=(40, 38, 46, 255), width=wd); ld.line((x - k, ey + k, x + k, ey - k), fill=(40, 38, 46, 255), width=wd)
    # 腮红 + 嘴
    for sgn in (-1, 1):
        ld.ellipse((cx + sgn * W * .22 - W * .04, cy + H * .04, cx + sgn * W * .22 + W * .04, cy + H * .08), fill=(240, 150, 150, 120))
    if eyes != 'x':
        ld.arc((cx - W * .05, cy + H * .02, cx + W * .05, cy + H * .09), 0, 180, fill=(40, 38, 46, 255), width=int(W * .014))
    else:
        ld.line((cx - W * .05, cy + H * .08, cx + W * .05, cy + H * .08), fill=(40, 38, 46, 255), width=int(W * .014))
    # 手
    hand = pose.get('hand')
    if hand:
        hx = cx + W * .36; hy = {'up': H * .2, 'wave1': H * .16, 'wave2': H * .26}[hand]
        ld.line((cx + W * .28, cy + H * .1, hx, hy), fill=c['dark'] + (255,), width=int(W * .04))
        ld.ellipse((hx - W * .06, hy - W * .06, hx + W * .06, hy + W * .06), fill=c['base'] + (255,), outline=c['dark'] + (255,), width=int(W * .012))
    # 道具
    prop = pose.get('prop')
    if prop == 'glass':
        gx, gy, gr = cx + W * .3, cy + H * .02, W * .12
        ld.ellipse((gx - gr, gy - gr, gx + gr, gy + gr), fill=(200, 230, 255, 90), outline=(60, 60, 70, 255), width=int(W * .025))
        ld.line((gx + gr * .7, gy + gr * .7, gx + gr * 1.5, gy + gr * 1.5), fill=(60, 60, 70, 255), width=int(W * .04))
    elif prop == 'keys':
        ld.rounded_rectangle((cx - W * .3, H * .78, cx + W * .3, H * .9), radius=int(W * .03), fill=(60, 60, 66, 255))
        for i in range(6):
            ld.rectangle((cx - W * .26 + i * W * .09, H * .8, cx - W * .26 + i * W * .09 + W * .06, H * .86), fill=(120, 200, 180, 255) if (i + pose.get('k', 0)) % 3 == 0 else (150, 150, 158, 255))
    elif prop == 'sweat':
        ld.polygon([(cx + W * .3, cy - H * .16), (cx + W * .26, cy - H * .06), (cx + W * .34, cy - H * .06)], fill=(120, 180, 240, 230))
    elif prop == 'zz':
        txt = ImageDraw.Draw(layer)
        for i, (dx, dy, sz) in enumerate([(.28, -.36, .07), (.36, -.44, .05)]):
            txt.text((cx + dx * W, cy + dy * H), 'z', fill=(90, 90, 100, 255), font_size=int(W * sz))
    # 变换：缩放 / 倾斜 / 位移 / 躺倒
    sx, sy = pose.get('sx', 1), pose.get('sy', 1)
    if sx != 1 or sy != 1:
        nw, nh = int(W * sx), int(H * sy)
        layer = layer.resize((nw, nh), Image.LANCZOS)
        pad = Image.new('RGBA', (W, H), (0, 0, 0, 0)); pad.alpha_composite(layer, ((W - nw) // 2, H - nh)); layer = pad
    if pose.get('lie'):
        layer = layer.rotate(80, resample=Image.BICUBIC, center=(W // 2, int(H * .8)))
        pad = Image.new('RGBA', (W, H), (0, 0, 0, 0)); pad.alpha_composite(layer, (0, int(H * .1))); layer = pad
    elif pose.get('tilt'):
        layer = layer.rotate(pose['tilt'], resample=Image.BICUBIC, center=(W // 2, int(H * .86)))
    img.alpha_composite(layer, (0, -int(pose.get('dy', 0) * H)))
    return img.resize((CW, CH), Image.LANCZOS)


def poses(anim, n):
    out = []
    for i in range(n):
        t = i / n
        if anim == 'idle':
            out.append(dict(sy=1 + .02 * math.sin(2 * math.pi * t), eyes='closed' if i == 4 else 'open'))
        elif anim in ('running-right', 'running-left'):
            out.append(dict(dy=abs(math.sin(math.pi * t * 2)) * .05, tilt=(-8 if anim == 'running-right' else 8), sx=1.02, sy=.98, eyes='open'))
        elif anim == 'waving':
            out.append(dict(hand='wave1' if i % 2 == 0 else 'wave2', eyes='happy'))
        elif anim == 'jumping':
            out.append(dict(dy=[0, .12, .2, .12, 0][i] if n == 5 else .1, sy=[.94, 1.06, 1.02, 1.06, .94][i] if n == 5 else 1, eyes='happy'))
        elif anim == 'failed':
            out.append(dict(lie=i >= 2, eyes='x', prop='sweat' if i < 2 else None, tilt=[0, 30][i] if i < 2 else 0))
        elif anim == 'waiting':
            out.append(dict(eyes='closed', sy=1 + .015 * math.sin(2 * math.pi * t), prop='zz' if i % 2 == 0 else None))
        elif anim == 'running':
            out.append(dict(prop='keys', k=i, dy=.01 * (i % 2), eyes='open'))
        elif anim == 'review':
            out.append(dict(prop='glass', tilt=6 * math.sin(2 * math.pi * t), eyes='open'))
    return out


def build(name, out_dir: Path):
    sheet = Image.new('RGBA', (CW * COLS, CH * ROWS), (0, 0, 0, 0))
    for row, (anim, n) in enumerate(ANIM):
        for col, pose in enumerate(poses(anim, n)):
            sheet.alpha_composite(draw_char(name, pose), (col * CW, row * CH))
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet.save(out_dir / f'{name}.webp', 'WEBP', lossless=True, quality=90)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(Path(__file__).resolve().parent / 'sprites'))
    a = ap.parse_args()
    for name in CHARS:
        build(name, Path(a.out))
        print(' ', name)
    print(f'{len(CHARS)} 只已生成到 {a.out}')


if __name__ == '__main__':
    main()
