"""Proposal only: uv run --with "pillow>=10.1,<14" python docs/art/build_callout_proposal.py."""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT=Path(__file__).with_name('callout-proposal')
BG,INK,SHADOW,MID,LIGHT='#16201b','#020504','#7f948a','#9fb3a9','#d3ece0'
PALETTE={INK,SHADOW,MID,LIGHT}  # Board ground is never a component colour.


def frame():
    im=Image.new('RGBA',(16,16)); d=ImageDraw.Draw(im)
    d.line([(2,0),(13,0),(15,2),(15,13),(13,15),(2,15),(0,13),(0,2),(2,0)],fill=INK)
    d.line([(2,1),(13,1),(14,2),(14,13),(13,14),(2,14),(1,13),(1,2),(2,1)],fill=SHADOW)
    d.line([(3,1),(12,1)],fill=MID)
    d.line([(1,3),(1,12)],fill=MID)
    return im


def nine_slice(src,width,height):
    assert src.size==(16,16) and width>=16 and height>=16
    out=Image.new('RGBA',(width,height))
    xs,ys=[0,4,12,16],[0,4,12,16]
    tx,ty=[0,4,width-4,width],[0,4,height-4,height]
    for y in range(3):
        for x in range(3):
            tile=src.crop((xs[x],ys[y],xs[x+1],ys[y+1]))
            tile=tile.resize((tx[x+1]-tx[x],ty[y+1]-ty[y]),Image.Resampling.NEAREST)
            out.paste(tile,(tx[x],ty[y]))
    return out


def leaders():
    images={}
    paths={'horizontal':[(0,8),(15,8)],'vertical':[(8,0),(8,15)],
           'elbow':[(8,0),(8,8),(15,8)],'diagonal':[(0,15),(15,0)]}
    for name,points in paths.items():
        im=Image.new('RGBA',(16,16)); d=ImageDraw.Draw(im)
        d.line(points,fill=INK,width=3); d.line(points,fill=SHADOW,width=1)
        images['leader-'+name]=im
    cap=Image.new('RGBA',(4,4)); d=ImageDraw.Draw(cap)
    d.rectangle((0,0,3,3),fill=INK); d.rectangle((1,1,2,2),fill=LIGHT)
    images['leader-cap']=cap
    return images


def text(board,xy,value,size=12,colour=LIGHT):
    mask=Image.new('1',board.size)
    ImageDraw.Draw(mask).text(xy,value,font=ImageFont.load_default(size=size),fill=1)
    board.paste(colour,mask=mask)


def validate(images):
    assert set(images)=={'frame','leader-horizontal','leader-vertical','leader-elbow','leader-diagonal','leader-cap'}
    for name,im in images.items():
        assert im.mode=='RGBA'
        alpha=set(im.getchannel('A').getdata())
        assert alpha<={0,255} and 255 in alpha, 'binary alpha / nonempty component'
        if name!='leader-cap': assert 0 in alpha, 'transparent component background'
        assert {'#%02x%02x%02x'%p[:3] for p in im.getdata() if p[3]}<=PALETTE, name
    assert images['frame'].getpixel((8,8))[3]==0, 'frame centre must remain transparent'
    for w,h in ((104,44),(156,56),(156,100),(240,128)):
        rendered=nine_slice(images['frame'],w,h)
        for x,y,dx,dy in ((0,0,0,0),(12,0,w-4,0),(0,12,0,h-4),(12,12,w-4,h-4)):
            assert rendered.crop((dx,dy,dx+4,dy+4)).tobytes()==images['frame'].crop((x,y,x+4,y+4)).tobytes(), 'corner stretched'


def sample(images):
    board=Image.new('RGB',(920,600),BG)
    text(board,(20,16),'MONTNEY / PIXEL CALLOUT PROPOSAL',22)
    text(board,(20,48),'Proposal only / synthetic values / no map or font change',12,MID)
    d=ImageDraw.Draw(board)
    for points in [[(96,282),(96,222),(152,222),(152,184)],
                   [(316,304),(316,228)],[(536,272),(536,184)]]:
        d.line(points,fill=INK,width=3); d.line(points,fill=SHADOW,width=1)
        x,y=points[0]; d.rectangle((x-2,y-2,x+2,y+2),fill=INK)
        d.rectangle((x-1,y-1,x,y),fill=LIGHT)
    for x,y,w,h,rows in [(20,128,156,56,[('GENERATION','420 MW')]),
                        (236,128,156,100,[('GAS RECEIPTS','1,250 MMcf/d'),('CAPABILITY','1,600 MMcf/d')]),
                        (448,128,156,56,[('MISSING / STALE','-- MW')])]:
        d.rectangle((x+2,y,x+w-3,y+h-1),fill=INK)
        d.rectangle((x,y+2,x+w-1,y+h-3),fill=INK)
        border=nine_slice(images['frame'],w,h); board.paste(border,(x,y),border)
        for row,(lab,value) in enumerate(rows):
            text(board,(x+10,y+8+row*44),lab,10,MID)
            text(board,(x+10,y+23+row*44),value,18 if 'MMcf' not in value else 14)
    text(board,(650,128),'FRAME BEHAVIOUR',12)
    for i,line in enumerate(['4 px fixed corners','1 px inner keyline','No pulsing border','No flow arrow on leader','Existing text retained']):
        text(board,(650,154+i*22),line,12,MID)
    text(board,(20,340),'9-SLICE SOURCE / native16 + 6x / inset4',12)
    for xy,factor in [((20,375),1),((60,365),6)]:
        im=images['frame'].resize((16*factor,16*factor),Image.Resampling.NEAREST)
        board.paste(im,xy,im)
    for i,name in enumerate(['leader-horizontal','leader-vertical','leader-elbow','leader-diagonal','leader-cap']):
        x=240+i*132; im=images[name]
        board.paste(im,(x,375),im)
        enlarged=im.resize((im.width*3,im.height*3),Image.Resampling.NEAREST)
        board.paste(enlarged,(x,408),enlarged)
        text(board,(x,470),name.replace('leader-','').upper(),10,MID)
    text(board,(20,536),'PROPOSAL: neutral stepped frame + unobtrusive leader. P4a placement/metrics stay authoritative.',12,MID)
    text(board,(20,560),'Text here is illustrative; no pixel font supplied. Erik approval required before integration.',12,MID)
    return board


def main():
    images={'frame':frame(),**leaders()}; validate(images)
    board=sample(images)
    OUT.mkdir(parents=True,exist_ok=True)
    for name,im in images.items():
        im.save(OUT/(name+'.png'),optimize=True)
        im.resize((im.width*2,im.height*2),Image.Resampling.NEAREST).save(OUT/(name+'@2x.png'),optimize=True)
    meta={'status':'frame-approved','approval':'L-041 Q10, 2026-11-01','frame':{'file':'frame.png','size':[16,16],'slice':[4,4,4,4],
          'borderCssPx':4,'centre':'transparent','minimumCssSize':[16,16]},
          'leaders':{'tileSize':[16,16],'strokeCssPx':1,'casingCssPx':3,
          'cap':{'size':[4,4],'centreSize':[2,2],'outlineCssPx':1},
          'stretch':'horizontal/vertical axis only; elbows/caps fixed; diagonal repeat at 1:1'},
          'density2':'exact nearest-neighbour 2x; display at original logical dimensions'}
    (OUT/'slices.json').write_text(json.dumps(meta,indent=2)+'\n',encoding='utf-8')
    board.save(OUT/'sample.png',optimize=True)
    print(f'6 proposal components + @2x; 4 render sizes preserve corners; sample {board.width}x{board.height}')


if __name__=='__main__': main()
