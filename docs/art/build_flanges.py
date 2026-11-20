"""Build gas flange symbols: uv run --with "pillow>=10.1,<14" python docs/art/build_flanges.py."""
import json
import copy
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'web/sprites'
HEIGHTS=(6,8,10,12,14)
INK,SHADOW='#020504','#7f948a'


def flange(height):
    assert height in HEIGHTS
    im=Image.new('RGBA',(3,height))
    for x in range(3):
        im.putpixel((x,0),(2,5,4,255))
        im.putpixel((x,1),(127,148,138,255))
        im.putpixel((x,height-2),(127,148,138,255) if x==0 else (2,5,4,255))
        im.putpixel((x,height-1),(2,5,4,255))
    return im


def validate(atlas,index,high,index2):
    assert set(index)=={f'line/flange-{h}' for h in HEIGHTS}, 'five required heights'
    assert set(index2)==set(index) and atlas.mode==high.mode=='RGBA'
    assert high.size==(atlas.width*2,atlas.height*2)
    assert high.tobytes()==atlas.resize(high.size,Image.Resampling.NEAREST).tobytes(), '2x pixels'
    occupied=set()
    for h in HEIGHTS:
        key=f'line/flange-{h}'; box=index[key]
        x,y,w,height=(box[k] for k in ('x','y','width','height'))
        assert w==3 and height==h and box['pixelRatio']==1, 'native dimensions'
        assert x>=2 and y>=2 and x+w<=atlas.width-2 and y+h<=atlas.height-2, 'bounds'
        assert index2[key]==dict(x=x*2,y=y*2,width=6,height=h*2,pixelRatio=2), '2x index'
        cells={(cx,cy) for cx in range(x-2,x+w+2) for cy in range(y-2,y+h+2)}
        assert not occupied.intersection(cells), 'gutter overlap'
        occupied.update(cells)
        im=atlas.crop((x,y,x+w,y+h))
        for py in range(h):
            for px in range(3):
                pixel=im.getpixel((px,py))
                if 2<=py<h-2:
                    assert pixel==(0,0,0,0), 'transparent centre'
                else:
                    assert pixel in ((2,5,4,255),(127,148,138,255)), 'collar palette/alpha'


def build():
    atlas=Image.new('RGBA',(40,18)); index={}
    for i,h in enumerate(HEIGHTS):
        x=2+i*7; y=2
        atlas.paste(flange(h),(x,y))
        index[f'line/flange-{h}']=dict(x=x,y=y,width=3,height=h,pixelRatio=1)
    index2={k:dict(x=v['x']*2,y=v['y']*2,width=6,height=v['height']*2,pixelRatio=2) for k,v in index.items()}
    high=atlas.resize((80,36),Image.Resampling.NEAREST)
    validate(atlas,index,high,index2)
    return atlas,index,high,index2


def sample():
    board=Image.new('RGB',(760,420),'#16201b'); d=ImageDraw.Draw(board)
    def text(xy,value,size=12,colour='#d3ece0'):
        mask=Image.new('1',board.size)
        ImageDraw.Draw(mask).text(xy,value,font=ImageFont.load_default(size=size),fill=1)
        board.paste(colour,mask=mask)
    text((20,14),'MONTNEY / GAS FLANGE SYMBOLS',22)
    text((20,46),'Agreed symbol overlay / no line-pattern / power & interties unchanged',12,'#9fb3a9')
    for i,h in enumerate(HEIGHTS):
        x=24+i*146
        text((x,86),f'3 x {h} PX')
        im=flange(h); board.paste(im,(x,126),im)
        large=im.resize((24,h*8),Image.Resampling.NEAREST)
        board.paste(large,(x+35,110),large)
        text((x,236),f'H={h} / 1x + 8x',10,'#9fb3a9')
    text((20,282),'SYNTHETIC FLOW (MMcf/d) / utilization remains the base-line colour',12)
    for i,(flow,util,colour) in enumerate([(0,0,'#7f948a'),(4000,0.6,'#eef6f2'),(8000,1,'#ff4d6d')]):
        x=24+i*242; y=340
        casing=4+7*flow/8000
        height=next(h for h in HEIGHTS if h>=casing+2)
        # Art sample only: integer stroke approximations are NOT MapLibre proof.
        d.line([(x,y),(x+190,y)],fill=INK,width=round(casing))
        d.line([(x,y),(x+190,y)],fill=colour,width=round(1.5+5.5*flow/8000))
        if flow:
            for dx in (32,96,160):
                im=flange(height); board.paste(im,(x+dx,y-height//2),im)
        text((x,364),f'flow {flow} / util {util}',11,'#9fb3a9')
    text((20,398),'Static reference only; P4c proves native width, spacing, gaps, colour and FPS on the real map.',11,'#9fb3a9')
    return board


def main():
    atlas,index,high,index2=build(); board=sample()
    if '--seeded-reds' in sys.argv:
        for fault in ('key','dimensions','centre','palette','alpha','density-index','density-pixels','gutters'):
            a,idx,hi,idx2=copy.deepcopy((atlas,index,high,index2))
            box=idx['line/flange-6']; x,y=box['x'],box['y']
            if fault=='key': idx.pop('line/flange-6')
            if fault=='dimensions': box['width']=4
            if fault=='centre': a.putpixel((x,y+2),(2,5,4,255))
            if fault=='palette': a.putpixel((x,y),(255,181,71,255))
            if fault=='alpha': a.putpixel((x,y),(2,5,4,127))
            if fault=='density-index': idx2['line/flange-6']['pixelRatio']=1
            if fault=='density-pixels': hi.putpixel((0,0),(2,5,4,255))
            if fault=='gutters':
                idx['line/flange-8']['x']=x
                idx2['line/flange-8']['x']=x*2
            if fault in ('centre','palette','alpha'): hi=a.resize(hi.size,Image.Resampling.NEAREST)
            try: validate(a,idx,hi,idx2)
            except AssertionError as err: print(f'SEEDED RED: {fault} rejected ({err})')
            else: raise AssertionError(f'{fault} accepted')
    OUT.mkdir(parents=True,exist_ok=True)
    atlas.save(OUT/'flanges.png',optimize=True)
    high.save(OUT/'flanges@2x.png',optimize=True)
    for name,data in [('flanges.json',index),('flanges@2x.json',index2)]:
        (OUT/name).write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    board.save(ROOT/'docs/art/flanges-sample.png',optimize=True)
    print('5 flanges: 3xH, H=6/8/10/12/14; exact @2x; 4 collar rows; transparent centres')
    for name in ('flanges.png','flanges@2x.png'): print(name,(OUT/name).stat().st_size,'bytes')


if __name__=='__main__': main()
