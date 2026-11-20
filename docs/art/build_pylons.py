"""uv run --with "pillow>=10.1,<14" python docs/art/build_pylons.py --seeded-reds"""
import copy
import json
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'web/sprites'
INK,SHADOW,MID=(2,5,4,255),(127,148,138,255),(159,179,169,255)


def build():
    im=Image.new('RGBA',(16,16)); rows=[(INK,MID,INK),(SHADOW,MID,SHADOW),(INK,SHADOW,INK)]
    for y,row in enumerate(rows):
        for x,pixel in enumerate(row):
            im.putpixel((x+2,y+2),pixel)
            im.putpixel((x+2,13-y),pixel)
    index={'line/pylon':dict(x=2,y=2,width=3,height=12,pixelRatio=1)}
    high=im.resize((32,32),Image.Resampling.NEAREST)
    index2={'line/pylon':dict(x=4,y=4,width=6,height=24,pixelRatio=2)}
    return im,index,high,index2


def verify(atlas,index,high,index2):
    assert atlas.mode==high.mode=='RGBA' and atlas.size==(16,16) and high.size==(32,32)
    assert index=={'line/pylon':dict(x=2,y=2,width=3,height=12,pixelRatio=1)}, 'native index'
    assert index2=={'line/pylon':dict(x=4,y=4,width=6,height=24,pixelRatio=2)}, '2x index'
    assert high.tobytes()==atlas.resize((32,32),Image.Resampling.NEAREST).tobytes(), '2x pixels'
    glyph=atlas.crop((2,2,5,14))
    assert glyph.tobytes()==glyph.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes(), 'top/bottom symmetry'
    assert glyph.tobytes()==glyph.transpose(Image.Transpose.FLIP_LEFT_RIGHT).tobytes(), 'side symmetry'
    assert {p[3] for p in glyph.getdata()}=={0,255}, 'binary alpha'
    for y in range(16):
        for x in range(16):
            pixel=atlas.getpixel((x,y))
            if 2<=x<5 and (2<=y<5 or 11<=y<14):
                assert pixel in {(2,5,4,255),(127,148,138,255),(159,179,169,255)}, 'accent palette'
            else: assert pixel==(0,0,0,0), 'corridor/gutters'


def sample(atlas):
    out=Image.new('RGB',(680,280),'#16201b'); d=ImageDraw.Draw(out)
    def text(xy,value,size=12,colour='#d3ece0'):
        mask=Image.new('1',out.size)
        ImageDraw.Draw(mask).text(xy,value,font=ImageFont.load_default(size=size),fill=1)
        out.paste(colour,mask=mask)
    text((20,14),'MONTNEY / SYMMETRIC PYLON CROSS-ARM',20)
    text((20,44),'One glyph for power + interties / line/pylon / no line-pattern',12,'#9fb3a9')
    glyph=atlas.crop((2,2,5,14))
    for x,factor in ((24,1),(70,2),(130,8)):
        im=glyph.resize((3*factor,12*factor),Image.Resampling.NEAREST)
        out.paste(im,(x,90),im)
    text((210,88),'3 x 12 native / 6 x 24 at2x',12)
    text((210,110),'Rows3-8: fully transparent',12,'#9fb3a9')
    text((210,132),'Same light in either line direction',12,'#9fb3a9')
    text((210,154),'Only #020504 / #7f948a / #9fb3a9',12,'#9fb3a9')
    for x in range(20,650):
        d.line([(x,218),(x,221)],fill='#020504')
        if (x-20)%16<10: d.line([(x,219),(x,220)],fill='#c9a2ff')
    for x in range(55,650,96): out.paste(glyph,(x,214),glyph)
    text((20,243),'Schematic raster study at96px spacing; real-map G1-G7 proof belongs to Frontend.',11,'#9fb3a9')
    return out


def main():
    args=build(); verify(*args)
    if '--seeded-reds' in sys.argv:
        for fault in ('palette','alpha','corridor','symmetry','gutter','index','2x'):
            a,i,h,i2=copy.deepcopy(args)
            if fault=='palette':
                a.putpixel((3,2),(211,236,224,255)); a.putpixel((3,13),(211,236,224,255))
            if fault=='alpha':
                a.putpixel((3,2),(159,179,169,127)); a.putpixel((3,13),(159,179,169,127))
            if fault=='corridor':
                a.putpixel((3,7),(2,5,4,255)); a.putpixel((3,8),(2,5,4,255))
            if fault=='symmetry': a.putpixel((3,2),(127,148,138,255))
            if fault=='gutter': a.putpixel((0,0),(2,5,4,255))
            if fault=='index': i2['line/pylon']['pixelRatio']=1
            if fault=='2x': h.putpixel((0,0),(2,5,4,255))
            else: h=a.resize((32,32),Image.Resampling.NEAREST)
            try: verify(a,i,h,i2)
            except AssertionError as err: print(f'SEEDED RED {fault}: {err}')
            else: raise AssertionError(f'{fault} accepted')
    a,i,h,i2=args; OUT.mkdir(parents=True,exist_ok=True)
    a.save(OUT/'pylons.png',optimize=True); h.save(OUT/'pylons@2x.png',optimize=True)
    for name,index in [('pylons.json',i),('pylons@2x.json',i2)]:
        (OUT/name).write_text(json.dumps(index,separators=(',',':'))+'\n',encoding='utf-8')
    sample(a).save(ROOT/'docs/art/pylons-sample.png',optimize=True)
    print('PASS: one3x12 glyph, symmetric, rows3-8 clear; exact6x24 @2x in16/32 atlases')


if __name__=='__main__': main()
