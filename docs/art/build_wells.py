"""uv run --with "pillow>=10.1,<14" python docs/art/build_wells.py"""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'web/sprites'
INK,SHADOW,MID,LIGHT='#020504','#7f948a','#9fb3a9','#d3ece0'
ALLOWED={(2,5,4,255),(127,148,138,255),(159,179,169,255),(211,236,224,255)}


def sprite(size,group=False):
    im=Image.new('RGBA',(size,size)); d=ImageDraw.Draw(im); s=size/16
    def pts(p): return [(round(x*s),round(y*s)) for x,y in p]
    def box(rect,fill,outline=INK): d.rectangle(tuple(round(v*s) for v in rect),fill=fill,outline=outline)
    def line(p,fill=LIGHT,width=1): d.line(pts(p),fill=fill,width=width)
    def wheel(rect):
        d.ellipse(tuple(round(v*s) for v in rect),fill=LIGHT,outline=INK)
        x0,y0,x1,y1=rect; cx=(x0+x1)/2; cy=(y0+y1)/2
        line([(cx,cy-1),(cx,cy+1)],SHADOW)
        line([(cx-1,cy),(cx+1,cy)],SHADOW)
        d.point(pts([(cx,cy)])[0],fill=INK)
    # Three handwheels and a central flanged tree, not an oil pumpjack.
    box((7,3,9,13),MID)
    line([(3,6),(8,6)],INK,3); line([(3,6),(8,6)],MID)
    line([(8,9),(13,9)],INK,3); line([(8,9),(13,9)],MID)
    wheel((6,1,10,5)); wheel((2,4,6,8)); wheel((10,7,14,11))
    box((5,11,11,12),SHADOW)
    line([(6,11),(10,11)],LIGHT)
    box((1 if group else 3,13,14 if group else 12,14),SHADOW)
    line([(2 if group else 4,13),(13 if group else 11,13)],MID)
    if group:
        # Neutral three-dot aggregation marker; not a literal well count or a live status.
        box((10,1,14,5),INK)
        for x,y in ((11,2),(13,2),(12,4)):
            r=pts([(x,y)])[0]
            if size==16: d.point(r,fill=LIGHT)
            else: d.rectangle((r[0],r[1],r[0]+1,r[1]+1),fill=LIGHT)
    if size==32:
        line([(7,6),(7,10)],LIGHT)
        line([(8,12),(8,13)],MID)
    if size==16:
        rim=Image.new('RGBA',im.size)
        rim.paste(SHADOW,(0,0,size,size),im.getchannel('A').filter(ImageFilter.MaxFilter(3)))
        im=Image.alpha_composite(rim,im)
    return im


def validate(a,index,high,index2):
    assert set(index)==set(index2)=={f'{kind}-{s}-static' for kind in ('gas-well','gas-pad') for s in (16,32)}
    assert high.size==tuple(n*2 for n in a.size)
    assert high.tobytes()==a.resize(high.size,Image.Resampling.NEAREST).tobytes()
    for key,r in index.items():
        size=int(key.split('-')[-2]); x,y=r['x'],r['y']
        assert r==dict(x=x,y=y,width=size,height=size,pixelRatio=1)
        assert index2[key]==dict(x=x*2,y=y*2,width=size*2,height=size*2,pixelRatio=2)
        im=a.crop((x,y,x+size,y+size))
        assert set(im.getchannel('A').getdata())=={0,255}
        assert all(p==(0,0,0,0) or p in ALLOWED for p in im.getdata())


def build():
    a=Image.new('RGBA',(120,36)); index={}; images={}; x=2
    for kind in ('gas-well','gas-pad'):
        for size in (16,32):
            key=f'{kind}-{size}-static'; im=sprite(size,kind=='gas-pad')
            a.paste(im,(x,2)); images[key]=im
            index[key]=dict(x=x,y=2,width=size,height=size,pixelRatio=1); x+=size+4
    high=a.resize((240,72),Image.Resampling.NEAREST)
    index2={k:dict(x=r['x']*2,y=r['y']*2,width=r['width']*2,height=r['height']*2,pixelRatio=2) for k,r in index.items()}
    validate(a,index,high,index2)
    return a,index,high,index2,images


def contact(images):
    out=Image.new('RGB',(860,390),'#16201b')
    def text(xy,value,size=12,colour=LIGHT):
        mask=Image.new('1',out.size)
        ImageDraw.Draw(mask).text(xy,value,font=ImageFont.load_default(size=size),fill=1)
        out.paste(colour,mask=mask)
    text((20,14),'MONTNEY / GAS WELL + PAD',22)
    text((20,46),'Christmas-tree wellhead / neutral group marker / static, no invented live activity',12,MID)
    for row,kind in enumerate(('gas-well','gas-pad')):
        y=100+row*125; text((20,y+18),kind.upper())
        for size,dx,factor in ((16,200,1),(32,245,1),(32,310,3),(16,500,3)):
            im=images[f'{kind}-{size}-static']
            im=im.resize((size*factor,size*factor),Image.Resampling.NEAREST)
            out.paste(im,(dx,y),im)
    text((200,77),'16px | 32px | 3x32 review',11,MID)
    text((500,77),'DPR2.6 / 48 device px',11,MID)
    text((620,110),'DPR2.6 loader:',12)
    text((620,134),'3 device pixels / art pixel',11,MID)
    text((620,156),'48/2.6 = 18.46 CSS px',11,MID)
    text((620,180),'No fractional image scaling',11,MID)
    text((20,365),'3-dot badge means grouped entity, not exactly 3 wells. Phone column is a device-pixel study, not a browser screenshot.',10,MID)
    return out


def main():
    a,i,h,i2,images=build(); OUT.mkdir(parents=True,exist_ok=True)
    a.save(OUT/'wells.png',optimize=True); h.save(OUT/'wells@2x.png',optimize=True)
    for name,data in [('wells.json',i),('wells@2x.json',i2)]:
        (OUT/name).write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    contact(images).save(ROOT/'docs/art/wells-sample.png',optimize=True)
    print('PASS:4 static well/pad images at16/32; house palette, binary alpha, exact @2x')
    for p in OUT.glob('wells*'): print(p.name,p.stat().st_size)


if __name__=='__main__': main()
