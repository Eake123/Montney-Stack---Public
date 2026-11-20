"""Check shipped files: uv run --with "pillow>=10.1,<14" python docs/art/verify_nodes.py.
Pass --seeded-reds to prove corruption is rejected (all mutations stay in memory).
"""
import copy
import json
import re
import sys
from pathlib import Path
from PIL import Image

ROOT=Path(__file__).resolve().parents[2]
HOUSE_PALETTE={'#16201b','#020504','#7f948a','#9fb3a9','#d3ece0','#b7c98a',
               '#3dffb8','#c9a2ff','#ffb547','#ff4d6d','#b392f0'}
REQUIRED=set('plant-combined-cycle plant-simple-cycle plant-cogen plant-gas-fired-steam '
    'plant-coal-to-gas plant-hydro plant-wind plant-solar plant-unknown receipt hub storage '
    'border compressor interconnect intertie lng-terminal lng-ship gas-processing meter '
    'region system warn critical unknown plant-nuclear plant-coal plant-battery plant-oil '
    'plant-biomass plant-geothermal plant-pumped-storage plant-gas'.split())
NEW_STATES={k:['glow0','glow1','glow2','glow3'] for k in ('plant-coal','plant-oil','plant-gas')}
NEW_STATES.update({'plant-battery':['static','charge','discharge'],
    **{k:['static'] for k in ('plant-biomass','plant-geothermal','plant-pumped-storage')}})
FRAME_KINDS=('plant-hydro','plant-solar','plant-nuclear','plant-biomass','plant-geothermal','plant-unknown')
for kind in FRAME_KINDS:
    NEW_STATES[kind]=['static']+[f'frame{i}' for i in range(4)]
    if kind in ('plant-biomass','plant-geothermal','plant-unknown'):
        NEW_STATES[kind] += [f'glow{i}' for i in range(4)]
for kind,prefixes in (('plant-battery',('charge','discharge')),('plant-pumped-storage',('pump','generate'))):
    for prefix in prefixes: NEW_STATES[kind] += [f'{prefix}{i}' for i in range(4)]


def verify(atlas, high, index, high_index, manifest):
    assert set(manifest)==REQUIRED, 'kind coverage'
    assert len(index)==238 and set(index)==set(high_index), 'image coverage'
    assert atlas.mode==high.mode=='RGBA'
    assert high.size==tuple(v*2 for v in atlas.size)
    assert high.tobytes()==atlas.resize(high.size,Image.Resampling.NEAREST).tobytes(), '@2x pixels'
    documented=set(re.findall(r'^\| `(#\w{6})` \|', (ROOT/'docs/art/STYLE.md').read_text(encoding='utf-8'), re.M))
    assert documented==HOUSE_PALETTE, 'documented house palette'
    # Identity colours no longer used by style.js dots remain in the approved house palette.
    allowed=HOUSE_PALETTE
    occupied=set(); referenced=set()
    for kind,entry in manifest.items():
        assert entry['anchor'] in ('bottom','center')
        assert set(entry['sizes'])=={'16','32'}
        assert entry['states']
        if kind.startswith('plant-'): assert entry['anchor']=='bottom', 'plant anchor'
        if kind in NEW_STATES:
            assert entry['anchor']=='bottom' and entry['states']==NEW_STATES[kind], 'new fuel contract'
        for size_key,state_keys in entry['sizes'].items():
            size=int(size_key)
            assert set(state_keys)==set(entry['states'])
            frames=[]; fuel_states={}
            for state,key in state_keys.items():
                assert key not in referenced, 'duplicate reference'
                referenced.add(key)
                box=index[key]; hi=high_index[key]
                x,y,w,h=(box[p] for p in ('x','y','width','height'))
                assert all(isinstance(v,int) for v in (x,y,w,h))
                assert w==h==size and box['pixelRatio']==1
                assert x>=2 and y>=2 and x+w<=atlas.width-2 and y+h<=atlas.height-2, 'rectangle bounds'
                assert hi==dict(x=x*2,y=y*2,width=w*2,height=h*2,pixelRatio=2), '@2x index'
                cells={(cx,cy) for cx in range(x-2,x+w+2) for cy in range(y-2,y+h+2)}
                assert not cells.intersection(occupied), 'overlapping gutters'
                occupied.update(cells)
                image=atlas.crop((x,y,x+w,y+h))
                pixels=list(image.getdata()); alpha={p[3] for p in pixels}
                assert alpha=={0,255}, 'binary alpha / nonempty image'
                colours={'#%02x%02x%02x'%p[:3] for p in pixels if p[3]}
                assert colours<=allowed, 'palette'
                assert '#3dffb8' not in colours or kind=='receipt', 'receipt jade'
                assert '#c9a2ff' not in colours or kind=='intertie', 'intertie purple'
                assert '#ffb547' not in colours or kind=='warn', 'warning amber'
                assert '#ff4d6d' not in colours or kind=='critical', 'critical red'
                assert '#b392f0' not in colours or kind=='storage', 'storage violet'
                if kind in NEW_STATES:
                    fuel_colours={'#020504','#7f948a','#9fb3a9','#d3ece0'}
                    if kind=='plant-gas': fuel_colours.add('#b7c98a')
                    assert colours<=fuel_colours, 'new fuel palette'
                    fuel_states[state]=image.tobytes()
                if size==32:
                    x0,y0,x1,y1=image.getbbox()
                    assert min(x0,y0)>=1 and max(x1,y1)<=31, '32px margin'
                if state.startswith('frame'): frames.append(image.tobytes())
            if kind in ('plant-wind','compressor'):
                assert len(frames)==len(set(frames))==4, 'four distinct animation frames'
            if kind in NEW_STATES:
                for prefix in ('frame','glow','charge','discharge','pump','generate'):
                    seq=[fuel_states.get(f'{prefix}{i}') for i in range(4)]
                    if any(v is not None for v in seq):
                        assert all(v is not None for v in seq) and len(set(seq))==4, 'distinct fuel states'
                if kind=='plant-battery':
                    assert len({fuel_states[s] for s in ('static','charge','discharge')})==3, 'distinct fuel states'
                    assert all(fuel_states[f'charge{i}']!=fuel_states[f'discharge{i}'] for i in range(4)), 'signed frames'
                if kind=='plant-pumped-storage':
                    assert all(fuel_states[f'pump{i}']!=fuel_states[f'generate{i}'] for i in range(4)), 'signed frames'
    assert referenced==set(index), 'orphan image'
    return len(index)


def main():
    out=ROOT/'web/sprites'
    args=[Image.open(out/'nodes.png'), Image.open(out/'nodes@2x.png')]
    args += [json.loads((out/name).read_text()) for name in ('nodes.json','nodes@2x.json','nodes-manifest.json')]
    count=verify(*args)
    print(f'PASS: {len(REQUIRED)} kinds; {count} rectangles, palette, binary alpha, gutters, @2x and distinct frames')
    if '--seeded-reds' in sys.argv:
        palette_faults={
            'outside-house':('plant-cogen-16-glow0','#eef6f2','palette'),
            'amber-plant':('plant-cogen-16-glow0','#ffb547','warning amber'),
            'red-plant':('plant-cogen-16-glow0','#ff4d6d','critical red'),
            'storage-hub':('hub-16-static','#b392f0','storage violet'),
            'jade-hub':('hub-16-static','#3dffb8','receipt jade'),
            'purple-hub':('hub-16-static','#c9a2ff','intertie purple'),
        }
        for fault in ['coverage','bounds','density-index','2x-pixels','duplicate-frame',
                      'battery-contract','duplicate-battery','duplicate-glow','fuel-palette',
                      'duplicate-solar','signed-pump','plant-anchor',*palette_faults]:
            a,h,idx,hi,meta=copy.deepcopy(args)
            first=next(iter(idx))
            if fault=='coverage': meta.pop('meter')
            if fault=='bounds': idx[first]['x']=a.width
            if fault=='density-index': hi[first]['pixelRatio']=1
            if fault=='2x-pixels': h.putpixel((0,0),(255,0,255,255))
            if fault=='battery-contract': meta['plant-battery']['states']=['static']
            if fault=='plant-anchor': meta['plant-hydro']['anchor']='center'
            if fault in ('duplicate-solar','signed-pump'):
                kind,src_state,dst_state=('plant-solar','frame0','frame3') if fault=='duplicate-solar' else ('plant-pumped-storage','pump0','generate0')
                src=idx[f'{kind}-16-{src_state}']; dst=idx[f'{kind}-16-{dst_state}']
                a.paste(a.crop((src['x'],src['y'],src['x']+16,src['y']+16)),(dst['x'],dst['y']))
                h=a.resize(h.size,Image.Resampling.NEAREST)
            if fault in ('duplicate-battery','duplicate-glow'):
                kind,src_state,dst_state=('plant-battery','charge','discharge') if fault=='duplicate-battery' else ('plant-coal','glow0','glow3')
                src=idx[f'{kind}-16-{src_state}']; dst=idx[f'{kind}-16-{dst_state}']
                a.paste(a.crop((src['x'],src['y'],src['x']+16,src['y']+16)),(dst['x'],dst['y']))
                h=a.resize(h.size,Image.Resampling.NEAREST)
            if fault=='fuel-palette':
                box=idx['plant-battery-16-static']
                a.putpixel((box['x']+8,box['y']+8),(183,201,138,255))
                h=a.resize(h.size,Image.Resampling.NEAREST)
            if fault=='duplicate-frame':
                src=idx['compressor-16-frame0']; dst=idx['compressor-16-frame3']
                im=a.crop((src['x'],src['y'],src['x']+16,src['y']+16))
                a.paste(im,(dst['x'],dst['y']))
                h=a.resize(h.size,Image.Resampling.NEAREST)
            if fault in palette_faults:
                key,colour,expected=palette_faults[fault]
                box=idx[key]
                a.putpixel((box['x']+8,box['y']+8),tuple(bytes.fromhex(colour[1:]))+(255,))
                h=a.resize(h.size,Image.Resampling.NEAREST)
            try: verify(a,h,idx,hi,meta)
            except AssertionError as err:
                if fault in palette_faults: assert str(err)==expected, (fault,str(err),expected)
                print(f'SEEDED RED: {fault} rejected ({err})')
            else: raise AssertionError(f'{fault} accepted')


if __name__=='__main__': main()
