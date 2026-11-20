"""uv run --with "pillow>=10.1,<14" python docs/art/build_power_lines.py"""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'web/sprites'
KINDS=('power-tower','intertie-tower','power-joint','intertie-joint','power-casing','intertie-casing')
INK,SHADOW,MID,LIGHT='#020504','#7f948a','#9fb3a9','#d3ece0'


def sprite(kind,size):
    im=Image.new('RGBA',(size,size)); d=ImageDraw.Draw(im); s=size/16
    def pts(points): return [(round(x*s),round(y*s)) for x,y in points]
    def line(points,colour=MID,width=1): d.line(pts(points),fill=colour,width=width)
    def box(rect,colour): d.rectangle(tuple(round(v*s) for v in rect),fill=colour)
    if kind=='power-tower':
        line([(3,14),(7,2),(9,2),(13,14)],INK,3)
        line([(3,14),(7,2),(9,2),(13,14)],MID)
        line([(2,4),(13,4)],INK,3); line([(2,4),(13,4)],LIGHT)
        line([(4,6),(11,6)],INK,3); line([(4,6),(11,6)],SHADOW)
        line([(5,11),(11,13),(4,13),(10,11)],SHADOW)
        line([(2,14),(5,14)],LIGHT); line([(11,14),(14,14)],LIGHT)
    elif kind=='intertie-tower':
        for x in (4,11):
            line([(x,2),(x,14)],INK,3); line([(x,2),(x,14)],MID)
            line([(x-1,14),(x+1,14)],LIGHT)
        for y in (3,5,11):
            line([(2,y),(13,y)],INK,3); line([(2,y),(13,y)],LIGHT if y==3 else SHADOW)
        box((6,1,9,2),LIGHT)
    elif kind.endswith('joint'):
        left,right=(5,10) if kind=='power-joint' else (3,12)
        for x in (left,right):
            line([(x,3),(x,13)],INK,3)
            line([(x-1,3),(x+1,3)],LIGHT)
            line([(x-1,13),(x+1,13)],LIGHT)
            line([(x,4),(x,12)],SHADOW)
        if kind=='intertie-joint':
            line([(6,2),(9,2)],MID); line([(6,14),(9,14)],MID)
    elif kind.endswith('casing'):
        # Neutral rail modules, delivered as symbols. Never line-pattern textures.
        for y in (4,12):
            line([(1,y),(14,y)],INK,3)
            line([(1,y),(14,y)],SHADOW)
        for x in ((2,13) if kind=='power-casing' else (2,6,10,13)):
            box((x,3,x,5),MID); box((x,11,x,13),MID)
    else: raise ValueError(kind)
    # Keep the original 4px power casing + 2px dash untouched at any phase.
    # Two extra pixels of clearance protect the tangent channel at native16.
    if not kind.endswith('tower'):
        gap=6 if size==16 else 12
        d.rectangle((0,(size-gap)//2,size-1,(size+gap)//2-1),fill=(0,0,0,0))
    # Outer source margin. Thick construction strokes must never leak into it.
    d.rectangle((0,0,size-1,0),fill=(0,0,0,0)); d.rectangle((0,size-1,size-1,size-1),fill=(0,0,0,0))
    d.rectangle((0,0,0,size-1),fill=(0,0,0,0)); d.rectangle((size-1,0,size-1,size-1),fill=(0,0,0,0))
    return im


def build():
    atlas=Image.new('RGBA',(256,112)); index={}; images={}
    x=y=2; shelf=0
    for kind in KINDS:
        for size in (16,32):
            if x+size+2>atlas.width: x=2; y+=shelf+4; shelf=0
            im=sprite(kind,size); key=f'line/{kind}-{size}'
            atlas.paste(im,(x,y)); images[key]=im
            index[key]=dict(x=x,y=y,width=size,height=size,pixelRatio=1)
            x+=size+4; shelf=max(shelf,size)
    atlas=atlas.crop((0,0,256,y+shelf+2))
    hi=atlas.resize((atlas.width*2,atlas.height*2),Image.Resampling.NEAREST)
    index2={k:{p:v[p]*2 for p in ('x','y','width','height')}|{'pixelRatio':2} for k,v in index.items()}
    return atlas,index,hi,index2,images


def board(images):
    out=Image.new('RGB',(1000,560),'#16201b')
    def text(xy,value,size=12,colour=LIGHT):
        mask=Image.new('1',out.size)
        ImageDraw.Draw(mask).text(xy,value,font=ImageFont.load_default(size=size),fill=1)
        out.paste(colour,mask=mask)
    text((20,16),'MONTNEY / POWER + INTERTIE LINE ART',22)
    text((20,48),'Static symbol studies / no line-pattern / native16 + native32 + 3x32',12,MID)
    for i,kind in enumerate(KINDS):
        x=20+(i%3)*326; y=100+(i//3)*200
        text((x,y),kind.upper())
        for size,dx,factor in ((16,0,1),(32,28,1),(32,96,3)):
            im=images[f'line/{kind}-{size}']
            im=im.resize((size*factor,size*factor),Image.Resampling.NEAREST)
            out.paste(im,(x+dx,y+38),im)
        text((x,y+150),'Off-line placement' if kind.endswith('tower') else 'Clear centre preserves wire/dashes',10,SHADOW)
    text((20,518),'4 neutral house colours / binary alpha / off-line towers + clear-centre joints/casing',12,MID)
    text((20,540),'Art only; real-zoom samples are separate. Frontend must prove curves, crossings, colours and frame cost.',11,MID)
    return out


def main():
    from verify_power_lines import verify
    atlas,index,hi,index2,images=build()
    verify(atlas,index,hi,index2)
    OUT.mkdir(parents=True,exist_ok=True)
    atlas.save(OUT/'power-lines.png',optimize=True); hi.save(OUT/'power-lines@2x.png',optimize=True)
    for name,data in [('power-lines.json',index),('power-lines@2x.json',index2)]:
        (OUT/name).write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    board(images).save(ROOT/'docs/art/power-lines-sample.png',optimize=True)
    print(f'12 symbols, native16/32 + exact @2x; atlas {atlas.width}x{atlas.height}')


if __name__=='__main__': main()
