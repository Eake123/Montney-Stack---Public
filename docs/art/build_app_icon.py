"""uv run --with "pillow>=10.1,<14" python docs/art/build_app_icon.py"""
import io
import struct
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'web/sprites/app-icon'
INK,SHADOW,MID,LIGHT,JADE='#020504','#7f948a','#9fb3a9','#d3ece0','#3dffb8'
PALETTE={(2,5,4,255),(127,148,138,255),(159,179,169,255),(211,236,224,255),(61,255,184,255)}


def master(size=16):
    im=Image.new('RGBA',(size,size)); d=ImageDraw.Draw(im); s=size/16
    # Folded map: three stepped panels and the same upper-left light as the map art.
    for points,colour in [([(1,5),(5,3),(5,12),(1,14)],SHADOW),
                          ([(5,3),(10,5),(10,14),(5,12)],LIGHT),
                          ([(10,5),(14,3),(14,12),(10,14)],MID)]:
        d.polygon([(round(x*s),round(y*s)) for x,y in points],fill=colour,outline=INK)
    # Dark casing keeps the brand's jade pipe legible on the folded map.
    route=[(3,11),(7,11),(7,9),(11,9),(11,6)]
    route=[(round(x*s),round(y*s)) for x,y in route]
    d.line(route,fill=INK,width=5 if size==48 else 3)
    d.line(route,fill=JADE,width=3 if size==48 else 1)
    # Cool jade gas flame, not an amber warning or a red alert.
    flame=[(11,1),(11,3),(13,2),(14,5),(13,7),(10,7),(9,5),(10,3)]
    d.polygon([(round(x*s),round(y*s)) for x,y in flame],fill=JADE,outline=INK)
    d.line([(round(x*s),round(y*s)) for x,y in [(10,4),(10,5),(11,6)]],fill=LIGHT,width=1)
    return im


def ico_bytes(frames):
    # Embed exact PNG frames; never let an ICO encoder resample the pixel art.
    blobs=[]
    for im in frames:
        data=io.BytesIO(); im.save(data,format='PNG',optimize=True); blobs.append(data.getvalue())
    offset=6+16*len(frames); entries=[]
    for im,blob in zip(frames,blobs):
        w,h=im.size
        entries.append(struct.pack('<BBBBHHII',w if w<256 else 0,h if h<256 else 0,0,0,1,32,len(blob),offset))
        offset+=len(blob)
    return struct.pack('<HHH',0,1,len(frames))+b''.join(entries)+b''.join(blobs)


def validate(im):
    assert im.mode=='RGBA'
    assert set(im.getchannel('A').getdata())=={0,255}
    assert all(p==(0,0,0,0) or p in PALETTE for p in im.getdata())


def contact(frames):
    out=Image.new('RGB',(920,390),'#16201b'); d=ImageDraw.Draw(out)
    def text(xy,value,size=12,colour=LIGHT):
        mask=Image.new('1',out.size)
        ImageDraw.Draw(mask).text(xy,value,font=ImageFont.load_default(size=size),fill=1)
        out.paste(colour,mask=mask)
    text((20,14),'MONTNEY / MAP + PIPELINE + GAS',22)
    text((20,46),'Native favicon16/32/48 + Windows256 / crisp pixels / transparent corners',12,MID)
    for i,size in enumerate((16,32,48)):
        x=28+i*130; text((x,84),str(size)+' PX')
        for y,bg in ((112,'#16201b'),(206,LIGHT)):
            d.rectangle((x-8,y-8,x+size+7,y+size+7),fill=bg,outline=SHADOW)
            out.paste(frames[size],(x,y),frames[size])
    enlarged=frames[16].resize((128,128),Image.Resampling.NEAREST)
    out.paste(enlarged,(420,112),enlarged)
    text((420,260),'16 PX / 8X',11,MID)
    out.paste(frames[256],(624,78),frames[256])
    text((624,344),'256 PX DESKTOP / NATIVE',11,MID)
    text((20,352),'Two backdrops show browser contrast. No smoothing, shadows or new palette entries.',11,MID)
    return out


def main():
    frames={s:master(s) for s in (16,32,48)}
    frames[256]=frames[32].resize((256,256),Image.Resampling.NEAREST)
    for im in frames.values(): validate(im)
    OUT.mkdir(parents=True,exist_ok=True)
    for size in (16,32,48): frames[size].save(OUT/f'favicon-{size}.png',optimize=True)
    (OUT/'favicon.ico').write_bytes(ico_bytes([frames[s] for s in (16,32,48)]))
    (OUT/'montney-desktop.ico').write_bytes(ico_bytes([frames[s] for s in (16,32,48,256)]))
    for name,sizes in [('favicon.ico',(16,32,48)),('montney-desktop.ico',(16,32,48,256))]:
        decoded=Image.open(OUT/name)
        assert decoded.info['sizes']=={(s,s) for s in sizes}
        for size in sizes:
            im=decoded.ico.getimage((size,size)).convert('RGBA'); validate(im)
            assert im.tobytes()==frames[size].tobytes()
    contact(frames).save(ROOT/'docs/art/app-icon-sample.png',optimize=True)
    print('PASS: PNG16/32/48, favicon ICO16/32/48, desktop ICO16/32/48/256; exact palette/alpha/pixels')
    for p in OUT.iterdir(): print(p.name,p.stat().st_size,'bytes')


if __name__=='__main__': main()
