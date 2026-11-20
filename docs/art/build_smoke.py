"""uv run --with "pillow>=10.1,<14" python docs/art/build_smoke.py"""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'web/sprites'
INK,SHADOW,MID,LIGHT='#020504','#7f948a','#9fb3a9','#d3ece0'
PALETTE={(2,5,4,255),(127,148,138,255),(159,179,169,255),(211,236,224,255)}


def puff(size,frame,low=False):
    im=Image.new('RGBA',(size,size)); d=ImageDraw.Draw(im); s=size/16
    def pts(points): return [(round(x*s),round(y*s)) for x,y in points]
    def shape(points,fill=MID): d.polygon(pts(points),fill=fill,outline=INK)
    def line(points,fill=LIGHT): d.line(pts(points),fill=fill,width=1)
    # Shared stationary outlet. The large puff rises while a new bud forms below it.
    if low:
        shape([(7,14),(7,12),(8,11),(9,12),(8,14)],SHADOW)
        cx,cy=8+(frame//2),10-2*frame
        shape([(cx-1,cy-2),(cx+1,cy-2),(cx+2,cy-1),(cx+2,cy+1),
               (cx+1,cy+2),(cx-1,cy+2),(cx-2,cy+1),(cx-2,cy-1)],SHADOW)
        line([(cx-1,cy-1),(cx,cy-1)],MID)
    else:
        shape([(6,14),(6,12),(7,10),(9,10),(10,12),(9,14)],SHADOW)
        cx,cy=8+(frame//2),10-2*frame
        shape([(cx-2,cy-2),(cx-1,cy-3),(cx+1,cy-3),(cx+2,cy-2),
               (cx+3,cy-2),(cx+4,cy-1),(cx+4,cy+1),(cx+3,cy+2),
               (cx+1,cy+2),(cx,cy+3),(cx-2,cy+3),(cx-3,cy+2),
               (cx-3,cy),(cx-4,cy-1),(cx-3,cy-2)],MID)
        line([(cx-2,cy-1),(cx-1,cy-2),(cx+1,cy-2)])
        line([(cx+2,cy+1),(cx+3,cy+1)],SHADOW)
    if size==16:
        halo=Image.new('RGBA',im.size)
        halo.paste(SHADOW,(0,0,size,size),im.getchannel('A').filter(ImageFilter.MaxFilter(3)))
        im=Image.alpha_composite(halo,im)
    return im


def validate(atlas,index,high,index2):
    assert set(index)==set(index2)=={f'smoke/{variant}-{size}-{f}'
        for variant in ('normal','low') for size in (16,32) for f in range(4)}
    assert high.tobytes()==atlas.resize(high.size,Image.Resampling.NEAREST).tobytes()
    assert high.size==(atlas.width*2,atlas.height*2)
    for key,rect in index.items():
        size=int(key.split('-')[-2]); x,y=rect['x'],rect['y']
        assert rect==dict(x=x,y=y,width=size,height=size,pixelRatio=1)
        assert index2[key]==dict(x=x*2,y=y*2,width=size*2,height=size*2,pixelRatio=2)
        im=atlas.crop((x,y,x+size,y+size))
        assert set(im.getchannel('A').getdata())=={0,255}
        assert all(p==(0,0,0,0) or p in PALETTE for p in im.getdata())


def build():
    atlas=Image.new('RGBA',(256,104)); index={}; images={}; x=y=2; shelf=0
    for variant in ('normal','low'):
        for size in (16,32):
            for f in range(4):
                if x+size+2>atlas.width: x=2; y+=shelf+4; shelf=0
                key=f'smoke/{variant}-{size}-{f}'; im=puff(size,f,variant=='low')
                atlas.paste(im,(x,y)); images[key]=im
                index[key]=dict(x=x,y=y,width=size,height=size,pixelRatio=1)
                x+=size+4; shelf=max(shelf,size)
    atlas=atlas.crop((0,0,256,y+shelf+2))
    high=atlas.resize((atlas.width*2,atlas.height*2),Image.Resampling.NEAREST)
    index2={k:dict(x=v['x']*2,y=v['y']*2,width=v['width']*2,height=v['height']*2,pixelRatio=2) for k,v in index.items()}
    validate(atlas,index,high,index2)
    return atlas,index,high,index2,images


def contact(images):
    out=Image.new('RGB',(900,520),'#16201b')
    def text(xy,value,size=12,colour=LIGHT):
        mask=Image.new('1',out.size)
        ImageDraw.Draw(mask).text(xy,value,font=ImageFont.load_default(size=size),fill=1)
        out.paste(colour,mask=mask)
    text((20,14),'MONTNEY / FRESH GAS-PLANT ACTIVITY',22)
    text((20,46),'Four rising-puff phases / neutral greys / no fresh positive MW = no smoke',12,MID)
    for row,(variant,size) in enumerate((('normal',16),('normal',32),('low',16),('low',32))):
        y=90+row*94; text((20,y+14),f'{variant.upper()} / {size} PX')
        for f in range(4):
            x=190+f*170; im=images[f'smoke/{variant}-{size}-{f}']
            out.paste(im,(x,y+12),im)
            factor=3 if size==16 else 2
            big=im.resize((size*factor,size*factor),Image.Resampling.NEAREST)
            out.paste(big,(x+40,y),big)
    text((20,486),'Native + integer enlargement. Activity symbol, not measured pollution or emissions.',12,MID)
    return out


def main():
    atlas,index,high,index2,images=build(); OUT.mkdir(parents=True,exist_ok=True)
    atlas.save(OUT/'smoke.png',optimize=True); high.save(OUT/'smoke@2x.png',optimize=True)
    for name,data in [('smoke.json',index),('smoke@2x.json',index2)]:
        (OUT/name).write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    contact(images).save(ROOT/'docs/art/smoke-sample.png',optimize=True)
    print(f'16 images; two variants x four frames x two sizes; atlas {atlas.width}x{atlas.height}')


if __name__=='__main__': main()
