"""uv run --with "pillow>=10.1,<14" python docs/art/build_plant_motion.py.

Review shipped nodes/smoke pixels, not a second implementation of the drawings.
"""
import json
from pathlib import Path
from PIL import Image
from build_nodes import KINDS, GLOW_KINDS, board_text, LIGHT, MID

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'docs/art'
MOUTHS={
    'plant-combined-cycle':((11,2),(23,4)), 'plant-simple-cycle':((11,2),(23,4)),
    'plant-cogen':((8,3),(17,6)), 'plant-gas-fired-steam':((4,3),(9,6)),
    'plant-coal-to-gas':((11,2),(23,4)), 'plant-coal':((11,2),(22,4)),
    'plant-oil':((12,2),(24,4)), 'plant-gas':((4,2),(8,4)),
}


def sequences():
    result=[]
    for kind in KINDS:
        if not kind.startswith('plant-'): continue
        if kind in GLOW_KINDS: result.append((kind,'smoke'))
        elif kind=='plant-battery': result += [(kind,'charge'),(kind,'discharge')]
        elif kind=='plant-pumped-storage': result += [(kind,'pump'),(kind,'generate')]
        else: result.append((kind,'frame'))
    return result


def load():
    data={}
    for name in ('nodes','smoke'):
        atlas=Image.open(ROOT/f'web/sprites/{name}.png')
        index=json.loads((ROOT/f'web/sprites/{name}.json').read_text())
        for key,r in index.items():
            x,y,w,h=(r[k] for k in ('x','y','width','height'))
            data[key]=atlas.crop((x,y,x+w,y+h))
    return data


def composite(data,kind,seq,size,f,low=False,glow=2):
    # Node keeps its exact native size and bottom point, even on the larger review canvas.
    unit=size//16
    im=Image.new('RGBA',(24*unit,32*unit))
    state=f'glow{glow}' if seq=='smoke' else f'{seq}{f}'
    im.alpha_composite(data[f'{kind}-{size}-{state}'],(4*unit,16*unit))
    if seq=='smoke':
        x,y=MOUTHS[kind][size==32]
        im.alpha_composite(data[f'smoke/{"low" if low else "normal"}-{size}-{f}'],
                           (4*unit+x-size//2,16*unit+y+2*unit-size))
    return im


def sheet(data,rows,title):
    out=Image.new('RGB',(1550,90+len(rows)*235),'#16201b')
    board_text(out,(20,12),title,size=22)
    board_text(out,(20,44),'Four phases: each cell = native16 / native32 / 3x16 / 3x32. Positive-output study; not map proof.',MID)
    for row,(kind,seq) in enumerate(rows):
        y=80+235*row
        board_text(out,(15,y+10),kind,size=11)
        board_text(out,(15,y+30),seq,MID,11)
        for f in range(4):
            x=200+f*330
            board_text(out,(x,y),str(f),size=11)
            for size,dx,factor in ((16,0,1),(32,28,1),(16,80,3),(32,154,3)):
                im=composite(data,kind,seq,size,f)
                im=im.resize((im.width*factor,im.height*factor),Image.Resampling.NEAREST)
                out.paste(im,(x+dx,y+225-im.height),im)
    return out


def main():
    data=load(); rows=sequences()
    sheet(data,rows,'PLANT MOTION / ALL 17 KINDS, BOTH SIGNED DIRECTIONS').save(OUT/'plant-motion-sheet.png',optimize=True)
    # Convenient small review pages of the exact same samples.
    for i in range(0,len(rows),7):
        sheet(data,rows[i:i+7],f'PLANT MOTION / PAGE {i//7+1}').save(OUT/f'plant-motion-{i//7+1}.png',optimize=True)
    frames=[]
    for f in range(4):
        board=Image.new('RGB',(1000,700),'#16201b')
        board_text(board,(20,10),'PLANTS ON / 4 PHASES AT 4 Hz / ART PREVIEW',size=20)
        for i,(kind,seq) in enumerate(rows):
            x=15+(i%5)*198; y=55+(i//5)*158
            board_text(board,(x,y),kind,size=10)
            board_text(board,(x,y+16),seq,MID,10)
            for size,dx,factor in ((16,0,1),(32,30,1),(16,88,3)):
                im=composite(data,kind,seq,size,f)
                im=im.resize((im.width*factor,im.height*factor),Image.Resampling.NEAREST)
                board.paste(im,(x+dx,y+145-im.height),im)
        frames.append(board)
    # Fixed palette; no quantizer/dither can create colours or blur sprite edges.
    colors=['#16201b','#020504','#7f948a','#9fb3a9','#d3ece0','#b7c98a']
    pal=Image.new('P',(1,1)); rgb=[v for c in colors for v in bytes.fromhex(c[1:])]
    pal.putpalette(rgb+[0]*(768-len(rgb)))
    frames=[im.quantize(palette=pal,dither=Image.Dither.NONE) for im in frames]
    frames[0].save(OUT/'plant-motion.gif',save_all=True,append_images=frames[1:],duration=250,loop=0,optimize=False,disposal=2)
    for p in OUT.glob('plant-motion*'): print(p.name,p.stat().st_size)


if __name__=='__main__': main()
