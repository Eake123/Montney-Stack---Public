"""uv run --with "pillow>=10.1,<14" python docs/art/build_wildfire.py"""
import json
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'web/sprites'
INK,SHADOW,MID,LIGHT,FIRE='#020504','#7f948a','#9fb3a9','#d3ece0','#ff4d6d'
PALETTE={(2,5,4,255),(127,148,138,255),(159,179,169,255),(211,236,224,255),(255,77,109,255)}


def flame(size,frame,large):
    im=Image.new('RGBA',(size,size)); d=ImageDraw.Draw(im); s=size/16
    def pts(p): return [(round(x*s),round(y*s)) for x,y in p]
    def shape(p,colour): d.polygon(pts(p),fill=colour,outline=INK)
    def line(p,colour): d.line(pts(p),fill=colour,width=1)
    if large:
        tips=[(7,2,11,5),(9,1,5,5),(8,3,12,4),(6,1,10,3)]
        tx,ty,sx,sy=tips[frame]
        shape([(2,12),(2,9),(4,6),(4,9),(6,6),(tx,ty),(tx+1,6),
               (sx,sy),(12,8),(13,6),(14,10),(14,12),(12,14),(4,14)],FIRE)
        shape([(5,12),(6,10),(7,11),(8,7+frame%2),(9,10),(11,11),(10,13),(6,13)],LIGHT)
        line([(4,14),(12,14)],SHADOW)
    else:
        tx,ty=[(7,6),(9,5),(8,7),(7,5)][frame]
        shape([(5,12),(5,10),(6,8),(7,10),(tx,ty),(10,9),(11,11),(10,13),(6,14)],FIRE)
        shape([(7,12),(8,10),(9,12),(8,13)],LIGHT)
        line([(6,14),(9,14)],SHADOW)
    if size==16:
        halo=Image.new('RGBA',im.size)
        halo.paste(SHADOW,(0,0,size,size),im.getchannel('A').filter(ImageFilter.MaxFilter(3)))
        im=Image.alpha_composite(halo,im)
    return im


def validate(atlas,index,high,index2):
    assert set(index)==set(index2)=={f'wildfire/{kind}-{size}-{f}'
        for kind in ('hotspot','perimeter') for size in (16,32) for f in range(4)}
    assert high.size==(atlas.width*2,atlas.height*2)
    assert high.tobytes()==atlas.resize(high.size,Image.Resampling.NEAREST).tobytes()
    for key,r in index.items():
        size=int(key.split('-')[-2]); x,y=r['x'],r['y']
        assert r['width']==r['height']==size and r['pixelRatio']==1
        assert index2[key]==dict(x=x*2,y=y*2,width=size*2,height=size*2,pixelRatio=2)
        im=atlas.crop((x,y,x+size,y+size))
        assert set(im.getchannel('A').getdata())=={0,255}
        assert all(p==(0,0,0,0) or p in PALETTE for p in im.getdata())


def build():
    atlas=Image.new('RGBA',(256,104)); index={}; images={}; x=y=2; shelf=0
    for kind in ('hotspot','perimeter'):
        for size in (16,32):
            for frame in range(4):
                if x+size+2>256: x=2; y+=shelf+4; shelf=0
                key=f'wildfire/{kind}-{size}-{frame}'; im=flame(size,frame,kind=='perimeter')
                atlas.paste(im,(x,y)); images[key]=im
                index[key]=dict(x=x,y=y,width=size,height=size,pixelRatio=1)
                x+=size+4; shelf=max(shelf,size)
    atlas=atlas.crop((0,0,256,y+shelf+2))
    high=atlas.resize((atlas.width*2,atlas.height*2),Image.Resampling.NEAREST)
    index2={k:dict(x=v['x']*2,y=v['y']*2,width=v['width']*2,height=v['height']*2,pixelRatio=2) for k,v in index.items()}
    validate(atlas,index,high,index2)
    return atlas,index,high,index2,images


def contact(images):
    out=Image.new('RGB',(930,550),'#16201b'); d=ImageDraw.Draw(out)
    def text(xy,value,size=12,colour=LIGHT):
        mask=Image.new('1',out.size)
        ImageDraw.Draw(mask).text(xy,value,font=ImageFont.load_default(size=size),fill=1)
        out.paste(colour,mask=mask)
    text((20,14),'MONTNEY / ACTIVE WILDFIRE SPRITES',22)
    text((20,46),'Four flicker phases / small hotspot + larger active perimeter / native + integer enlargement',12,MID)
    for row,(kind,size) in enumerate((('hotspot',16),('hotspot',32),('perimeter',16),('perimeter',32))):
        y=88+row*96; text((20,y+16),f'{kind.upper()} / {size} PX')
        for frame in range(4):
            x=210+frame*170; im=images[f'wildfire/{kind}-{size}-{frame}']
            out.paste(im,(x,y+12),im)
            factor=3 if size==16 else 2
            big=im.resize((size*factor,size*factor),Image.Resampling.NEAREST)
            out.paste(big,(x+40,y),big)
    # Compare actual neighbouring UI colours, never baked into the flame assets.
    html=(ROOT/'web/index.html').read_text(encoding='utf-8')
    matrix=[float(v) for v in re.search(r'<filter\b[^>]*id="tint-gas"[^>]*>.*?values="([^"]+)"',html,re.S)[1].split()]
    rgba=(159,179,169,255,255)  # frame keyline #9fb3a9, plus SVG's offset term
    rgb=[max(0,min(255,round(sum(matrix[row*5+i]*rgba[i] for i in range(5))))) for row in range(3)]
    border='#%02x%02x%02x'%tuple(rgb)
    for x,colour,label in [(20,FIRE,'FIRE / #ff4d6d'),(300,'#ffb547','ALERT / #ffb547'),(610,border,'GAS KEYLINE / '+border)]:
        d.rectangle((x,486,x+20,502),fill=colour); text((x+28,487),label,10,MID)
    text((20,526),'Fire glyph identity is not alert severity or measured flame temperature/area.',11,MID)
    return out


def main():
    a,i,h,i2,images=build(); OUT.mkdir(parents=True,exist_ok=True)
    a.save(OUT/'wildfire.png',optimize=True); h.save(OUT/'wildfire@2x.png',optimize=True)
    for name,data in [('wildfire.json',i),('wildfire@2x.json',i2)]:
        (OUT/name).write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    contact(images).save(ROOT/'docs/art/wildfire-sample.png',optimize=True)
    print('PASS:16 images, hotspot/perimeter x four phases x16/32; palette/binary alpha/exact @2x')
    for p in OUT.glob('wildfire*'): print(p.name,p.stat().st_size)


if __name__=='__main__': main()
