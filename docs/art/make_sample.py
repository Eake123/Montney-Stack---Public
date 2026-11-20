"""Reproduce the static approval board:
uv run --with "pillow>=10.1,<14" python docs/art/make_sample.py
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HEX = ['#16201b', '#020504', '#7f948a', '#9fb3a9', '#d3ece0',
       '#b7c98a', '#3dffb8', '#c9a2ff', '#ffb547', '#ff4d6d']
BG, INK, SHADE, MID, LIGHT, SAGE, JADE, POWER, WARN, CRIT = HEX


def sprite(kind, size):
    im = Image.new('RGBA', (size, size))
    d = ImageDraw.Draw(im)
    s = size / 16

    def points(coords):
        return [(round(x*s), round(y*s)) for x, y in coords]

    def box(coords, fill, outline=INK):
        d.rectangle(tuple(round(v*s) for v in coords), fill=fill, outline=outline)

    def line(coords, fill=LIGHT):
        d.line(points(coords), fill=fill, width=1)

    def poly(coords, fill):
        d.polygon(points(coords), fill=fill, outline=INK)

    if kind == 'GAS PLANT':
        box((4, 3, 6, 9), MID)
        box((10, 2, 12, 9), SHADE)
        line([(4, 3), (5, 3)])
        line([(10, 2), (11, 2)])
        poly([(1, 9), (5, 7), (14, 7), (14, 14), (1, 14)], SAGE)
        poly([(11, 9), (14, 7), (14, 14), (11, 14)], SHADE)
        line([(2, 9), (10, 9)])
        box((3, 11, 5, 12), LIGHT, None)
        box((7, 11, 9, 12), MID, None)
        if size == 32:
            line([(2, 10), (10, 10)], LIGHT)
            line([(12, 10), (12, 13)], INK)
    elif kind == 'WIND FARM':
        for x, y in [(11, 6), (5, 5)]:
            line([(x, y), (x, 13)], INK)
            line([(x-0.5, y+1), (x-0.5, 13)], MID)
            for tip in [(x, y-4), (x-3, y+2), (x+3, y+2)]:
                line([(x, y), tip], INK)
            for tip in [(x, y-3), (x-2, y+1), (x+2, y+1)]:
                line([(x, y), tip], LIGHT)
            d.point(points([(x, y)])[0], fill=MID)
        line([(3, 14), (7, 14)], INK)
        line([(9, 14), (13, 14)], INK)
    elif kind == 'COMPRESSOR':
        box((1, 11, 14, 13), SHADE)
        poly([(3, 7), (6, 5), (13, 5), (13, 11), (3, 11)], MID)
        poly([(10, 7), (13, 5), (13, 11), (10, 11)], SHADE)
        line([(4, 7), (9, 7)])
        d.ellipse(tuple(round(v*s) for v in (5, 8, 10, 13)), fill=SHADE, outline=INK)
        line([(6, 9), (8, 9)], LIGHT)
        line([(1, 12), (4, 12)], LIGHT)
        line([(11, 12), (14, 12)], LIGHT)
        if size == 32:
            box((11, 7, 12, 9), INK, None)
            line([(6, 11), (8, 11)], MID)
    else:
        box((11, 5, 13, 10), MID)
        box((11, 6, 12, 7), LIGHT, None)
        for x in (3, 7):
            d.ellipse(tuple(round(v*s) for v in (x, 7, x+3, 11)), fill=LIGHT, outline=INK)
            line([(x+2, 9), (x+2, 10)], MID)
        poly([(1, 11), (14, 11), (12, 14), (4, 14)], SHADE)
        line([(2, 11), (13, 11)], LIGHT)
        if size == 32:
            line([(5, 13), (11, 13)], MID)
    if size == 16:
        # L-028: spend the former transparent margin on an external 1 px halo;
        # preserve every existing body pixel and the 16x16 canvas.
        halo = Image.new('RGBA', im.size, SHADE)
        halo.putalpha(im.getchannel('A').filter(ImageFilter.MaxFilter(3)))
        im = Image.alpha_composite(halo, im)
    return im


def main():
    board = Image.new('RGB', (736, 408), BG)
    d = ImageDraw.Draw(board)
    def text(xy, value, colour=LIGHT, size=12):
        # Binary glyph mask prevents antialiased text introducing extra colours.
        mask = Image.new('1', board.size)
        ImageDraw.Draw(mask).text(xy, value, font=ImageFont.load_default(size=size), fill=1)
        board.paste(colour, mask=mask)

    text((20, 14), 'MONTNEY / PIXEL LANGUAGE', size=20)
    text((20, 42), 'P4b house style / approved 2026-11-01 / L-028', MID)
    for i, colour in enumerate(HEX):
        x = 20 + i*70
        d.rectangle((x, 68, x+51, 79), fill=colour)
        text((x, 84), colour, MID, 10)
    for col, kind in enumerate(['GAS PLANT', 'WIND FARM', 'COMPRESSOR', 'LNG SHIP']):
        x = 20 + col*180
        text((x, 114), kind)
        for size, y, scale in [(16, 150, 4), (32, 246, 3)]:
            im = sprite(kind, size)
            text((x, y-17), f'{size} PX / NATIVE + {scale}X', MID, 10)
            board.paste(im, (x, y+12), im)
            large = im.resize((size*scale, size*scale), Image.Resampling.NEAREST)
            board.paste(large, (x+48, y), large)
    text((20, 369), 'UPPER-LEFT LIGHT / 16 PX HALO / NEUTRAL PLANT GLOW', MID)
    text((20, 388), 'Art direction only: not an atlas, live map or animation preview.', MID, 10)
    target = Path(__file__).with_name('style-sample.png')
    actual = {'#%02x%02x%02x' % rgb for rgb in board.getdata()}
    assert actual == set(HEX), actual
    for kind in ['GAS PLANT', 'WIND FARM', 'COMPRESSOR', 'LNG SHIP']:
        for size in (16, 32):
            im = sprite(kind, size)
            assert set(im.getchannel('A').getdata()) <= {0, 255}
            if size == 32:
                assert im.getbbox()[0] >= 1 and im.getbbox()[1] >= 1
                assert im.getbbox()[2] <= size-1 and im.getbbox()[3] <= size-1
    board.save(target, optimize=True)
    print(f'{target}: {board.width}x{board.height}, {target.stat().st_size} bytes, '
          '10 exact palette colours; 8 native studies; 32 px margins verified')


if __name__ == '__main__':
    main()
