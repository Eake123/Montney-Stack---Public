/* Temporary art study only. Run in an owned Montney browser tab after setting
 * window.__powerArtInput = {png: <base64 PNG>, index: <power-lines.json>}.
 * No server/repo changes, no source value changes, no deployment or FPS claim.
 */
(async () => {
  const map = window.__map;
  if (!window.__ready || !map?.getLayer('power')) throw new Error('Montney map is not ready');
  window.__powerArtCleanup?.();
  const input = window.__powerArtInput;
  const guarded = ['gas','gas-casing','power','power-casing','areas-line'];
  const before = JSON.stringify(guarded.map((id) => map.getLayer(id).serialize()));
  const source = 'art-power-study', layers = [], imageIds = [];
  const image = new Image(); image.src = 'data:image/png;base64,' + input.png;
  await image.decode();
  const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
  const ctx = canvas.getContext('2d'); ctx.drawImage(image,0,0);
  for (const [id, r] of Object.entries(input.index)) {
    const key = 'art-study/' + id;
    map.addImage(key, ctx.getImageData(r.x,r.y,r.width,r.height), {pixelRatio:1});
    imageIds.push(key);
  }
  const network = await (await fetch('/api/network')).json();
  const counts = {power:0,intertie:0};
  const features = network.features.filter((f) => f.properties.system === 'power' && f.properties.kind === 'link');
  for (const f of features) {
    const family = f.properties.type === 'intertie' ? 'intertie' : 'power';
    f.properties.art_kind = family + '-' + ['tower','joint','casing'][counts[family]++ % 3];
  }
  map.addSource(source, {type:'geojson',data:{type:'FeatureCollection',features}});
  for (const tower of [false,true]) {
    const id = 'art-power-study-' + (tower ? 'towers' : 'accents'); layers.push(id);
    const imageAt = (size) => ['concat','art-study/line/',['get','art_kind'],'-'+size];
    map.addLayer({id,type:'symbol',source,
      filter: ['in',['get','art_kind'],['literal',tower
        ? ['power-tower','intertie-tower']
        : ['power-joint','intertie-joint','power-casing','intertie-casing']]],
      layout:{'symbol-placement':'line','symbol-spacing':160,
        'icon-image':['step',['zoom'],imageAt(16),8,imageAt(32)],
        'icon-anchor':'center','icon-size':1,'icon-padding':0,
        'icon-offset':tower ? ['step',['zoom'],['literal',[0,-12]],8,['literal',[0,-20]]] : [0,0],
        'icon-rotation-alignment':'map','icon-pitch-alignment':'viewport','icon-keep-upright':true,
        'icon-allow-overlap':true,'icon-ignore-placement':true}}, 'nodes');
  }
  const banner = document.createElement('div'); banner.id='art-power-study-note';
  Object.assign(banner.style,{position:'fixed',right:'12px',top:'68px',maxWidth:'330px',zIndex:1000,
    background:'#020504',color:'#d3ece0',padding:'9px',border:'1px solid #7f948a',font:'11px monospace'});
  const update = () => { banner.textContent='ART STUDY ONLY | zoom '+map.getZoom().toFixed(2)+
    ' | '+(map.getZoom()>=8?'32':'16')+' px | 160 px placement. Towers are schematic, not mapped facilities. Base lines unchanged.'; };
  update(); document.body.appendChild(banner); map.on('moveend',update);
  window.__powerArtCleanup = () => {
    for (const id of layers) if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource(source)) map.removeSource(source);
    for (const id of imageIds) if (map.hasImage(id)) map.removeImage(id);
    map.off('moveend',update); banner.remove(); delete window.__powerArtCleanup;
  };
  if (before !== JSON.stringify(guarded.map((id) => map.getLayer(id).serialize()))) throw new Error('Guarded base layer changed');
  map.triggerRepaint();
  return {features:features.length,counts,images:imageIds.length,baseLayersUnchanged:true};
})();
