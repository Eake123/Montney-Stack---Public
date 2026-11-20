"""uv run --with "pillow>=10.1,<14" python docs/art/verify_power_lines.py --seeded-reds"""
import copy
import json
import re
import sys
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[2]
ALLOWED={(2,5,4,255),(127,148,138,255),(159,179,169,255),(211,236,224,255)}
KINDS=('power-tower','intertie-tower','power-joint','intertie-joint','power-casing','intertie-casing')


def verify(atlas,index,high,index2):
    style=set(re.findall(r'#[0-9a-f]{6}',(ROOT/'web/style.js').read_text(encoding='utf-8')))
    assert {'#%02x%02x%02x'%p[:3] for p in ALLOWED} <= style, 'existing style colours'
    expected={f'line/{kind}-{size}' for kind in KINDS for size in (16,32)}
    assert set(index)==set(index2)==expected, 'coverage'
    assert atlas.mode==high.mode=='RGBA'
    assert high.size==tuple(v*2 for v in atlas.size)
    assert high.tobytes()==atlas.resize(high.size,Image.Resampling.NEAREST).tobytes(), '2x pixels'
    occupied=set(); rectangles=set()
    for key,v in index.items():
        size=int(key.rsplit('-',1)[1]); x,y,w,h=(v[p] for p in ('x','y','width','height'))
        assert w==h==size and v['pixelRatio']==1, 'size'
        assert x>=2 and y>=2 and x+w<=atlas.width-2 and y+h<=atlas.height-2, 'bounds'
        assert index2[key]==dict(x=x*2,y=y*2,width=w*2,height=h*2,pixelRatio=2), '2x index'
        cells={(px,py) for px in range(x-2,x+w+2) for py in range(y-2,y+h+2)}
        assert not occupied.intersection(cells), 'gutter overlap'; occupied.update(cells)
        rectangles.update((px,py) for px in range(x,x+w) for py in range(y,y+h))
        im=atlas.crop((x,y,x+w,y+h)); pixels=list(im.getdata())
        assert {p[3] for p in pixels}=={0,255}, 'binary alpha'
        assert all(p==(0,0,0,0) or p in ALLOWED for p in pixels), 'neutral palette'
        if '-tower-' not in key:
            gap=6 if size==16 else 12
            assert all(im.getpixel((px,py))==(0,0,0,0) for px in range(size)
                       for py in range((size-gap)//2,(size+gap)//2)), 'wire corridor'
        x0,y0,x1,y1=im.getbbox()
        assert min(x0,y0)>=1 and max(x1,y1)<=size-1, 'source margin'
    assert all(atlas.getpixel((x,y))==(0,0,0,0) for x in range(atlas.width)
               for y in range(atlas.height) if (x,y) not in rectangles), 'unused/gutter pixels'
    return len(index)


def main():
    p=ROOT/'web/sprites'
    args=[Image.open(p/'power-lines.png'),json.loads((p/'power-lines.json').read_text()),
          Image.open(p/'power-lines@2x.png'),json.loads((p/'power-lines@2x.json').read_text())]
    print(f'PASS: {verify(*args)} symbols; neutral palette, binary alpha, centre, bounds/gutters, @2x')
    if '--seeded-reds' in sys.argv:
        for fault in ('coverage','palette','alpha','corridor','size','density-index','density-pixels','margin','gutter-pixel'):
            a,idx,h,idx2=copy.deepcopy(args); key='line/power-tower-16'; v=idx[key]; x,y=v['x'],v['y']
            if fault=='coverage': idx.pop(key)
            if fault=='palette': a.putpixel((x+7,y+2),(255,181,71,255))
            if fault=='alpha': a.putpixel((x+7,y+2),(159,179,169,127))
            if fault=='corridor':
                v=idx['line/power-joint-16']
                a.putpixel((v['x']+8,v['y']+8),(159,179,169,255))
            if fault=='size': v['width']=15
            if fault=='density-index': idx2[key]['pixelRatio']=1
            if fault=='density-pixels': h.putpixel((0,0),(2,5,4,255))
            if fault=='margin': a.putpixel((x,y+2),(159,179,169,255))
            if fault=='gutter-pixel': a.putpixel((0,0),(159,179,169,255))
            if fault in ('palette','alpha','corridor','margin','gutter-pixel'): h=a.resize(h.size,Image.Resampling.NEAREST)
            try: verify(a,idx,h,idx2)
            except AssertionError as err: print(f'SEEDED RED {fault}: {err}')
            else: raise AssertionError(f'{fault} accepted')


if __name__=='__main__': main()
