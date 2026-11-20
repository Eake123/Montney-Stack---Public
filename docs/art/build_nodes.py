"""Build node atlases: uv run --with "pillow>=10.1,<14" python docs/art/build_nodes.py."""
import json
import math
import re
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'web/sprites'
INK, SHADOW, MID, LIGHT = '#020504', '#7f948a', '#9fb3a9', '#d3ece0'
SAGE, JADE, VIOLET, AMBER, RED, STORAGE = '#b7c98a', '#3dffb8', '#c9a2ff', '#ffb547', '#ff4d6d', '#b392f0'
PALETTE = [INK, SHADOW, MID, LIGHT, SAGE, JADE, VIOLET, AMBER, RED, STORAGE]
FUELS = ['combined-cycle', 'simple-cycle', 'cogen', 'gas-fired-steam', 'coal-to-gas', 'hydro', 'wind', 'solar']
KINDS = ['plant-'+f for f in FUELS] + ['plant-unknown', 'receipt', 'hub', 'storage',
    'border', 'compressor', 'interconnect', 'intertie', 'lng-terminal', 'lng-ship',
    'gas-processing', 'meter', 'region', 'system', 'warn', 'critical', 'unknown',
    'plant-nuclear', 'plant-coal', 'plant-battery', 'plant-oil', 'plant-biomass',
    'plant-geothermal', 'plant-pumped-storage', 'plant-gas']  # Append; retain old rectangles.
GLOW = [INK, SHADOW, MID, LIGHT]
GLOW_KINDS = ['plant-'+f for f in FUELS[:5]] + ['plant-coal','plant-oil','plant-gas']
ACTIVITY_KINDS = ['plant-hydro','plant-solar','plant-nuclear','plant-biomass',
                  'plant-geothermal','plant-unknown']
EXTRA_GLOW = ['plant-biomass','plant-geothermal','plant-unknown']


def halo(body):
    """Four-neighbour dilation, no diagonal filling; retain every body pixel."""
    assert body.size == (16, 16)
    x0, y0, x1, y1 = body.getbbox()
    assert min(x0, y0) >= 1 and max(x1, y1) <= 15, '16px body clips halo'
    alpha = body.getchannel('A')
    grown = alpha.copy()
    for dx, dy in ((1,0), (-1,0), (0,1), (0,-1)):
        shifted = Image.new('L', body.size)
        shifted.paste(alpha, (dx, dy))
        grown = ImageChops.lighter(grown, shifted)
    rim = Image.new('RGBA', body.size, SHADOW)
    rim.putalpha(grown)
    return Image.alpha_composite(rim, body)


def draw_body(kind, size, state='static'):
    im = Image.new('RGBA', (size, size))
    d = ImageDraw.Draw(im)
    scale = size / 16
    def xy(p): return [(round(x*scale), round(y*scale)) for x,y in p]
    def box(r, fill, outline=INK):
        d.rectangle(tuple(round(v*scale) for v in r), fill=fill, outline=outline)
    def poly(p, fill): d.polygon(xy(p), fill=fill, outline=INK)
    def line(p, fill=LIGHT): d.line(xy(p), fill=fill, width=1)
    def oval(r, fill=MID):
        d.ellipse(tuple(round(v*scale) for v in r), fill=fill, outline=INK)
    glow = GLOW[int(state[-1])] if state.startswith('glow') else INK
    frame = int(state[-1]) if state.startswith('frame') else 0
    if kind in ['plant-'+f for f in FUELS[:5]] + ['plant-unknown']:
        # Shared approved power-house language; fuel-specific silhouettes.
        stacks = {'plant-combined-cycle': (4,10), 'plant-simple-cycle': (10,),
                  'plant-cogen': (3,7), 'plant-gas-fired-steam': (3,),
                  'plant-coal-to-gas': (3,6,10), 'plant-unknown': ()}[kind]
        for x in stacks:
            box((x, 3 if x%2 else 2, x+2, 9), MID)
            line([(x, 3 if x%2 else 2), (x+1, 3 if x%2 else 2)])
        poly([(1,9),(5,7),(14,7),(14,14),(1,14)], SAGE if kind!='plant-unknown' else MID)
        poly([(11,9),(14,7),(14,14),(11,14)], SHADOW)
        line([(2,9),(10,9)])
        if kind=='plant-gas-fired-steam':
            poly([(7,5),(11,5),(13,9),(6,9)], MID)
        if kind=='plant-coal-to-gas':
            line([(2,11),(10,11)], SHADOW)
            line([(5,10),(5,12)], SHADOW)
        if kind=='plant-cogen':
            box((12,5,14,10), MID)
            line([(12,5),(14,5)])
        box((3,11,5,12), glow, None)
        box((7,11,9,12), glow, None)
        if kind=='plant-unknown':
            line([(6,5),(8,5),(8,6),(7,7)], LIGHT)
        if size==32:
            line([(2,10),(10,10)], LIGHT)
            line([(12,11),(12,13)], INK)
    elif kind=='plant-coal':
        box((10,2,12,10), MID); line([(10,2),(11,2)])
        box((7,8,14,14), MID); line([(8,8),(13,8)])
        box((9,11,12,12), glow, None)
        poly([(1,14),(2,12),(4,9),(6,10),(8,14)], SHADOW)
        line([(3,12),(4,11)], MID)
        if size==32: line([(5,12),(6,13)], INK)
    elif kind=='plant-oil':
        box((11,2,13,11), MID); line([(11,2),(12,2)])
        box((7,10,14,14), MID); line([(8,10),(13,10)])
        box((9,12,12,13), glow, None)
        box((3,5,7,11), MID)
        oval((1,5,5,11), MID)
        line([(3,6),(6,6)], LIGHT)
        line([(2,12),(6,12)], INK)
        if size==32: line([(6,8),(6,10)], SHADOW)
    elif kind=='plant-gas':
        # Generic inventory gas plant; does not imply CC, SC or cogeneration.
        box((3,2,5,9), MID); line([(3,2),(4,2)])
        poly([(1,10),(4,7),(12,7),(14,10),(14,14),(1,14)], SAGE)
        poly([(12,8),(14,10),(14,14),(12,14)], SHADOW)
        line([(3,9),(11,9)])
        box((3,11,5,12), glow, None); box((8,11,10,12), glow, None)
        if size==32: line([(3,10),(11,10)], LIGHT)
    elif kind=='plant-battery':
        # Two rows of containers, not a state-of-charge bar. Top arrow is signed power.
        box((1,8,14,14), SHADOW)
        for x in (2,6,10):
            box((x,9,x+3,13), MID)
            line([(x+1,10),(x+1,12)], LIGHT)
        line([(2,11),(13,11)], INK)
        line([(2,6),(13,6)], MID)
        if state=='charge':
            poly([(7,1),(9,1),(9,3),(11,3),(8,6),(5,3),(7,3)], LIGHT)
        elif state=='discharge':
            poly([(8,1),(11,4),(9,4),(9,6),(7,6),(7,4),(5,4)], LIGHT)
        else:
            box((6,2,10,4), SHADOW)
        if size==32:
            for x in (3,7,11): line([(x,13),(x+1,13)], INK)
    elif kind=='plant-biomass':
        box((12,2,14,9), MID); line([(12,2),(13,2)])
        poly([(1,9),(4,6),(4,9),(8,6),(8,9),(11,6),(14,9),(14,14),(1,14)], MID)
        line([(2,10),(12,10)], LIGHT)
        # Log ends distinguish the sawtooth fuel hall at native16.
        for x in (3,7):
            oval((x,11,x+3,14), SHADOW)
            d.point(xy([(x+1,12)])[0], LIGHT)
    elif kind=='plant-geothermal':
        # Static vent/well category mark, not a measured or animated emissions plume.
        poly([(2,14),(3,11),(6,11),(7,13),(11,12),(14,14)], SHADOW)
        box((6,7,9,12), MID); line([(7,8),(7,11)], LIGHT)
        box((4,7,11,8), MID)
        poly([(4,6),(3,4),(5,2),(6,3),(5,4),(6,6)], LIGHT)
        poly([(9,6),(8,4),(10,1),(12,2),(10,4),(11,6)], MID)
        if size==32: line([(3,13),(5,13)], MID)
    elif kind=='plant-pumped-storage':
        # Two separated reservoir basins connected by a diagonal penstock.
        line([(10,5),(10,8),(5,11)], INK)
        line([(11,5),(11,8),(6,11)], MID)
        poly([(6,2),(14,2),(13,6),(7,6)], SHADOW)
        line([(7,3),(13,3)], LIGHT); line([(8,4),(12,4)], MID)
        poly([(1,10),(9,10),(8,14),(2,14)], SHADOW)
        line([(2,11),(8,11)], LIGHT); line([(3,12),(7,12)], MID)
    elif kind=='plant-nuclear':
        # Symbolic paired reactor domes + turbine hall; no cooling towers or activity plume.
        for left, top, right in ((2,2,8), (9,4,14)):
            poly([(left,top+3),(left+1,top+1),(left+2,top),
                  (right-2,top),(right-1,top+1),(right,top+3),
                  (right,11),(left,11)], MID)
            line([(left+1,top+3),(left+2,top+1),(right-2,top+1)], LIGHT)
            line([(right-1,top+4),(right-1,10)], SHADOW)
        poly([(1,11),(4,9),(14,9),(14,14),(1,14)], MID)
        poly([(11,11),(14,9),(14,14),(11,14)], SHADOW)
        line([(2,11),(10,11)], LIGHT)
        box((3,12,5,13), INK, None)
        box((7,12,9,13), INK, None)
        if size==32:
            line([(4,4),(4,8)], LIGHT)
            line([(11,6),(11,8)], LIGHT)
            line([(12,12),(12,13)], INK)
    elif kind=='plant-wind':
        # Diagonal spacing + plus halo keeps the two farms readable at native16.
        for cx,cy,r,foot in [(11,9,2,14),(5,5,3,14)]:
            line([(cx,cy),(cx,foot)], MID)
            for blade in range(3):
                a=math.radians(-90 + blade*120 + frame*30)
                tip=(cx+round(r*math.cos(a)), cy+round(r*math.sin(a)))
                line([(cx,cy),tip], LIGHT)
            d.point(xy([(cx,cy)])[0], INK)
            line([(cx-1,foot),(cx+1,foot)], INK)
    elif kind=='plant-solar':
        box((4,10,5,14), MID); box((11,10,12,14), MID)
        poly([(3,4),(14,4),(12,11),(1,11)], SHADOW)
        line([(4,5),(12,5)], LIGHT)
        line([(3,8),(12,8)], MID)
        line([(7,5),(5,10)], MID); line([(11,5),(9,10)], MID)
    elif kind=='plant-hydro':
        poly([(4,4),(12,4),(14,14),(1,14)], MID)
        line([(4,5),(11,5)], LIGHT)
        for x in (4,8,11):
            line([(x,8),(x-1,13)], INK)
        line([(2,14),(13,14)], LIGHT)
    elif kind=='compressor':
        box((1,11,14,13), SHADOW)
        poly([(3,7),(6,5),(13,5),(13,11),(3,11)], MID)
        poly([(10,7),(13,5),(13,11),(10,11)], SHADOW)
        line([(4,7),(9,7)])
        oval((5,8,10,13), SHADOW); line([(6,9),(8,9)], LIGHT)
        if state.startswith('frame'):
            d.point(xy([(1+frame,12)])[0], LIGHT)
            d.point(xy([(11+frame,12)])[0], LIGHT)
    elif kind in ('lng-ship','lng-terminal'):
        box((11,5,13,10), MID); box((11,6,12,7), LIGHT, None)
        for x in (3,7):
            oval((x,7,x+3,11), LIGHT)
            line([(x+2,9),(x+2,10)], MID)
        if kind=='lng-ship':
            poly([(1,11),(14,11),(12,14),(4,14)], SHADOW)
            line([(2,11),(13,11)])
        else:
            box((1,11,14,14), SHADOW)
            line([(2,12),(13,12)])
            line([(2,3),(2,10)], MID); line([(2,3),(8,3),(8,5)], LIGHT)
    elif kind=='gas-processing':
        for x,h in ((3,4),(8,2),(12,6)):
            box((x,h,x+2,13), MID)
            line([(x,h),(x+1,h)], LIGHT)
            line([(x,h+3),(x+1,h+3)], SHADOW)
        box((1,12,14,14), SHADOW); line([(2,12),(13,12)])
    elif kind=='storage':
        box((3,6,12,12), STORAGE)
        oval((3,3,12,8), STORAGE)
        line([(4,6),(4,11)], LIGHT)
        line([(3,12),(5,14),(11,14),(12,12)], MID)
    elif kind=='receipt':
        box((2,11,14,14), SHADOW)
        line([(2,12),(13,12)], MID)
        poly([(5,2),(10,2),(10,7),(13,7),(8,11),(2,7),(5,7)], JADE)
    elif kind=='hub':
        for p in [[(8,2),(8,13)],[(2,8),(13,8)],[(3,3),(12,12)]]: line(p,MID)
        oval((4,4,11,11), LIGHT); oval((6,6,9,9), SHADOW)
    elif kind=='border':
        box((2,7,4,14), MID); box((11,7,13,14), MID)
        box((2,5,13,7), LIGHT)
        for x in (4,8,12): line([(x,5),(x,6)], INK)
        line([(7,9),(7,14)], LIGHT)
    elif kind in ('intertie','interconnect'):
        colour=VIOLET if kind=='intertie' else LIGHT
        box((2,5,5,11), MID); box((10,5,13,11), MID)
        line([(3,6),(3,10)], LIGHT)
        poly([(5,5),(9,5),(9,3),(13,7),(9,10),(9,8),(5,8)], colour)
        line([(2,13),(13,13)], SHADOW)
    elif kind=='meter':
        box((2,11,14,13), MID); box((4,9,11,12), MID)
        oval((4,3,11,10), LIGHT)
        line([(8,8),(10,5)], INK)
    elif kind in ('region','system'):
        if kind=='region':
            poly([(3,3),(11,2),(14,6),(12,12),(5,14),(1,9)], MID)
            poly([(4,4),(10,3),(13,6),(11,11),(5,13),(2,9)], INK)
            box((6,6,9,9), LIGHT)
        else:
            for x,y in [(3,3),(11,3),(7,11)]:
                line([(x,y),(7,7)], MID)
                box((x-1,y-1,x+1,y+1), LIGHT)
            oval((5,5,9,9), MID)
    elif kind in ('warn','critical'):
        colour=AMBER if kind=='warn' else RED
        if kind=='warn': poly([(8,2),(14,13),(1,13)], colour)
        else: poly([(5,2),(11,2),(14,5),(14,10),(10,14),(5,14),(1,10),(1,5)], colour)
        box((7,5,8,9), INK, None); box((7,11,8,12), INK, None)
    elif kind=='unknown':
        oval((2,2,13,13), MID)
        line([(5,5),(6,4),(9,4),(10,5),(10,7),(8,9)], INK)
        d.point(xy([(8,11)])[0], INK)
    else: raise ValueError(kind)
    return im


def validate_image(im, size, margin=False):
    assert im.mode=='RGBA' and im.size==(size,size)
    assert set(im.getchannel('A').getdata()) <= {0,255}, 'partial alpha'
    colours={'#%02x%02x%02x'%p[:3] for p in im.getdata() if p[3]}
    assert colours and colours <= set(PALETTE), colours
    if margin:
        x0,y0,x1,y1=im.getbbox()
        assert min(x0,y0)>=1 and max(x1,y1)<=size-1, 'body margin'


def legacy_states(kind):
    if kind in GLOW_KINDS: return ['glow'+str(i) for i in range(4)]
    if kind=='plant-battery': return ['static','charge','discharge']
    if kind in ('plant-wind','compressor'): return ['static']+['frame'+str(i) for i in range(4)]
    return ['static']


def added_states(kind):
    result=[]
    if kind in ACTIVITY_KINDS: result += [f'frame{i}' for i in range(4)]
    if kind in EXTRA_GLOW: result += [f'glow{i}' for i in range(4)]
    if kind in ('plant-battery','plant-pumped-storage'):
        for direction in (('charge','discharge') if kind=='plant-battery' else ('pump','generate')):
            result += [f'{direction}{i}' for i in range(4)]
    return result


def states(kind):
    return legacy_states(kind)+added_states(kind)


def activity_body(kind, size, state):
    """Full-node motion images; legacy drawings are never changed."""
    if state in legacy_states(kind): return draw_body(kind,size,state)
    base = state[:-1] if kind=='plant-battery' else 'static'
    im=draw_body(kind,size,base)
    d=ImageDraw.Draw(im); s=size/16
    def xy(p): return [(round(x*s),round(y*s)) for x,y in p]
    def line(p,fill=LIGHT,width=1): d.line(xy(p),fill=fill,width=width)
    def rect(r,fill): d.rectangle(tuple(round(v*s) for v in r),fill=fill)
    def poly(p,fill=MID): d.polygon(xy(p),fill=fill,outline=INK)
    f=int(state[-1])
    if state.startswith('glow'):
        if kind=='plant-unknown': return draw_body(kind,size,state)
        # New estimate-compatible steady output indicators. No timer or plume.
        if kind=='plant-biomass': rect((10,11,12,12),GLOW[f])
        else: rect((7,9,8,10),GLOW[f])
    elif kind=='plant-hydro':
        for x,phase in ((4,0),(8,2),(11,1)):
            y=8+(f+phase)%4
            line([(x,y),(x,y+1)],LIGHT,width=round(s))
    elif kind=='plant-solar':
        # A glint travels along the panel's sloped cell columns, never off the panel.
        x=4+2*f
        line([(x,6),(x-1,9)],LIGHT,width=round(s))
        line([(x+1,6),(x,9)],MID)
    elif kind in ('plant-nuclear','plant-unknown'):
        # Turbine/drive highlight advances through the machinery hall, not the reactor dome.
        x=3+2*f
        line([(x,11),(x,12)],LIGHT,width=round(s))
        line([(x+1,12),(x+1,13)],MID)
    elif kind=='plant-biomass':
        # Fuel feed conveyor: two moving teeth above the stored log ends.
        rect((2,9,11,10),INK)
        for x in (2+f,7+f): line([(x,9),(x+1,9)],LIGHT,width=round(s))
    elif kind=='plant-geothermal':
        rect((2,1,13,6),(0,0,0,0))
        for x,phase in ((5,0),(10,1)):
            y=5-(f+phase)%4
            poly([(x-1,y),(x,y-1),(x+1,y),(x+1,y+1),(x-1,y+1)],MID)
            d.point(xy([(x,y)])[0],LIGHT)
    elif kind=='plant-battery':
        # Arrow remains signed; highlights move down into / up out of container rows.
        y=(9+f) if state.startswith('charge') else (12-f)
        for x in (3,7,11):
            line([(x,y),(x+1,y)],LIGHT,width=round(s))
    elif kind=='plant-pumped-storage':
        # Arrow at the penstock + a moving water slug; reverse order alone is not enough.
        path=[(10,7),(10,8),(8,9),(6,10)]
        n=f if state.startswith('generate') else 3-f
        x,y=path[n]
        line([(x,y),(x+1,y)],LIGHT,width=round(s))
        if state.startswith('generate'):
            line([(12,7),(12,10),(11,9)],LIGHT)
            line([(12,10),(13,9)],LIGHT)
        else:
            line([(12,10),(12,7),(11,8)],LIGHT)
            line([(12,7),(13,8)],LIGHT)
    else: raise ValueError((kind,state))
    return im


def build():
    # The approved house palette survives removal of old dot colours from style.js.
    allowed=set(re.findall(r'^\| `(#\w{6})` \|',
        (ROOT/'docs/art/STYLE.md').read_text(encoding='utf-8'), re.M))
    assert set(PALETTE)<=allowed
    atlas=Image.new('RGBA',(512,512)); index={}; meta={}; images={}
    x=y=2; shelf=0
    for kind in KINDS:
        anchor = 'center' if kind in ('region','system','warn','critical','unknown','hub') else 'bottom'
        meta[kind]={'anchor':anchor, 'states':states(kind), 'sizes':{}}
        for size in (16,32): meta[kind]['sizes'][str(size)]={}
    # Pack all old images first, in their original order; append animations afterwards.
    for extra in (False,True):
      for kind in KINDS:
        for size in (16,32):
            for state in (added_states(kind) if extra else legacy_states(kind)):
                body=activity_body(kind,size,state)
                validate_image(body,size,margin=True)
                im=halo(body) if size==16 else body
                validate_image(im,size)
                key=f'{kind}-{size}-{state}'
                if x+size+2>atlas.width: x=2; y+=shelf+4; shelf=0
                assert y+size+2<=atlas.height, 'atlas overflow'
                atlas.paste(im,(x,y))
                index[key]={'x':x,'y':y,'width':size,'height':size,'pixelRatio':1}
                images[key]=im
                meta[kind]['sizes'][str(size)][state]=key
                x+=size+4; shelf=max(shelf,size)
    atlas=atlas.crop((0,0,512,y+shelf+2))
    index2={k:{'x':v['x']*2,'y':v['y']*2,'width':v['width']*2,
               'height':v['height']*2,'pixelRatio':2} for k,v in index.items()}
    for key,v in index.items():
        assert atlas.crop((v['x'],v['y'],v['x']+v['width'],v['y']+v['height'])).tobytes()==images[key].tobytes()
    return atlas,index,index2,meta,images


def board_text(board, xy, value, colour=LIGHT, size=12):
    mask=Image.new('1',board.size)
    ImageDraw.Draw(mask).text(xy,value,font=ImageFont.load_default(size=size),fill=1)
    board.paste(colour,mask=mask)


def sample(images):
    data_y = 80 + math.ceil(len(KINDS)/5)*154 + 15
    board=Image.new('RGB',(1000,data_y+335),'#16201b')
    board_text(board,(20,12),'MONTNEY / NODE ATLAS',size=22)
    board_text(board,(20,42),f'{len(KINDS)} silhouettes / native16 + native32 + 3x32 / plus halo at16',MID)
    for i,kind in enumerate(KINDS):
        x=20+(i%5)*198; y=80+(i//5)*154
        board_text(board,(x,y),kind.upper(),size=11)
        state='glow3' if states(kind)[0]=='glow0' else 'static'
        for size,dx,dy,factor in [(16,0,28,1),(32,24,20,1),(32,70,20,3)]:
            im=images[f'{kind}-{size}-{state}']
            im=im.resize((size*factor,size*factor),Image.Resampling.NEAREST)
            board.paste(im,(x+dx,y+dy),im)
    board_text(board,(20,data_y),'DATA STATES / plant glow0..3 | wind frame0..3 | compressor static + frame0..3',MID)
    for row,(kind,seq) in enumerate([('plant-combined-cycle',['glow0','glow1','glow2','glow3']),
                                    ('plant-wind',['frame0','frame1','frame2','frame3']),
                                    ('compressor',['frame0','frame1','frame2','frame3'])]):
        board_text(board,(20,data_y+35+row*80),kind,size=11)
        for i,state in enumerate(seq):
            for size,offset,factor in [(16,0,1),(32,20,1),(16,60,4)]:
                im=images[f'{kind}-{size}-{state}']
                im=im.resize((size*factor,size*factor),Image.Resampling.NEAREST)
                board.paste(im,(210+i*180+offset,data_y+20+row*80),im)
    board_text(board,(20,data_y+305),'Static contact sheet; not live map/FPS evidence. States: native16 / native32 / 4x16.',MID,11)
    return board


def nuclear_sample(images):
    board=Image.new('RGB',(780,330),'#16201b')
    board_text(board,(20,14),'ONTARIO / NUCLEAR PLANT',size=22)
    board_text(board,(20,45),'Before: unknown-fuel fallback / After: paired reactor domes + turbine hall',MID)
    for col,(kind,label) in enumerate((('plant-unknown','BEFORE / plant-unknown'),
                                     ('plant-nuclear','AFTER / plant-nuclear'))):
        x=20+col*380
        board_text(board,(x,82),label,size=15)
        for size,dx,factor in ((16,0,1),(32,40,1),(16,100,4),(32,190,4)):
            im=images[f'{kind}-{size}-static']
            im=im.resize((size*factor,size*factor),Image.Resampling.NEAREST)
            board.paste(im,(x+dx,120+128-im.height),im)
        board_text(board,(x,260),'native16 / native32 / 4x16 / 4x32',MID)
    board_text(board,(20,299),'Static / neutral house palette / bottom anchor / art comparison, not map proof',MID)
    return board


def plant_sample(images):
    kinds=[k for k in KINDS if k.startswith('plant-')]
    rows=math.ceil(len(kinds)/5); data_y=80+rows*150
    board=Image.new('RGB',(1100,data_y+390),'#16201b')
    board_text(board,(20,12),'REGIONS / EVERY PLANT FUEL',size=22)
    board_text(board,(20,44),'Each tile: native16 / native32 / 3x32. Glow shown at3; battery at rest. Art only.',MID)
    for i,kind in enumerate(kinds):
        x=20+(i%5)*216; y=80+(i//5)*150
        board_text(board,(x,y),kind,size=11)
        state='glow3' if kind in GLOW_KINDS else 'static'
        for size,dx,factor in ((16,0,1),(32,26,1),(32,72,3)):
            im=images[f'{kind}-{size}-{state}'].resize((size*factor,size*factor),Image.Resampling.NEAREST)
            board.paste(im,(x+dx,y+125-im.height),im)
    for row,kind in enumerate(('plant-coal','plant-oil','plant-gas','plant-battery')):
        y=data_y+row*80
        board_text(board,(20,y),kind,size=12)
        for i,state in enumerate(legacy_states(kind)):
            x=210+i*210
            board_text(board,(x,y),state,size=11)
            for size,dx,factor in ((16,0,1),(32,24,1),(16,70,3)):
                im=images[f'{kind}-{size}-{state}'].resize((size*factor,size*factor),Image.Resampling.NEAREST)
                board.paste(im,(x+dx,y+20),im)
    board_text(board,(20,data_y+350),'Coal/oil/gas:4 steady glow steps + shared smoke atlas. Battery:3 signed states, no timed loop.',MID)
    return board


def main():
    atlas,index,index2,meta,images=build()
    OUT.mkdir(parents=True,exist_ok=True)
    atlas.save(OUT/'nodes.png',optimize=True)
    atlas.resize((atlas.width*2,atlas.height*2),Image.Resampling.NEAREST).save(OUT/'nodes@2x.png',optimize=True)
    for name,data in [('nodes.json',index),('nodes@2x.json',index2),('nodes-manifest.json',meta)]:
        (OUT/name).write_text(json.dumps(data,separators=(',',':'))+'\n',encoding='utf-8')
    sample(images).save(ROOT/'docs/art/nodes-sample.png',optimize=True)
    nuclear_sample(images).save(ROOT/'docs/art/nuclear-before-after.png',optimize=True)
    plant_sample(images).save(ROOT/'docs/art/plants-sample.png',optimize=True)
    print(f'{len(KINDS)} kinds, {len(index)} indexed images; atlas {atlas.width}x{atlas.height}')
    for p in OUT.glob('nodes*'): print(p.name,p.stat().st_size)


if __name__=='__main__': main()
