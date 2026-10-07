/* Curated semantic colors. Contrast is checked by scripts/ui-foundation-gate.py. */
const UIPalettes = (() => {
  const items = [
    {
      id: "navy",
      en: "Midnight blue",
      vi: "Xanh hải quân",
      light: ["#275781", "#1d4263", "#eaf1f8"],
      dark: ["#9cc9f1", "#c2def8", "#20364b"],
    },
    {
      id: "teal",
      en: "Ocean teal",
      vi: "Xanh đại dương",
      light: ["#08675f", "#055149", "#e5f2ee"],
      dark: ["#83d6c7", "#b2e8de", "#183c38"],
    },
    {
      id: "indigo",
      en: "Quiet indigo",
      vi: "Chàm thanh lịch",
      light: ["#5052a0", "#3d3e7d", "#eeedf9"],
      dark: ["#b8b8f5", "#d5d5fc", "#30304f"],
    },
    {
      id: "forest",
      en: "Forest green",
      vi: "Xanh rừng",
      light: ["#356545", "#274d33", "#e9f2e9"],
      dark: ["#a3d3ad", "#c9e8cf", "#253c2b"],
    },
    {
      id: "copper",
      en: "Warm copper",
      vi: "Đồng ấm",
      light: ["#8a4c2e", "#6a3923", "#f8eee7"],
      dark: ["#e9b795", "#f4d5be", "#463126"],
    },
    {
      id: "graphite",
      en: "Graphite",
      vi: "Than chì",
      light: ["#303030", "#181818", "#ececec"],
      dark: ["#eeeeee", "#ffffff", "#303030"],
    },
  ];
  // Values here are palette primitives. CSS maps them to semantic/component tokens.
  const neutral = {
    light: {bg:'#f6f6f4',surface:'#ffffff','surface-2':'#f0f1ef','surface-3':'#e8e9e6',
      'nav-bg':'#f0f1ef',text:'#20252b','text-2':'#59616b','text-3':'#5c646e',
      line:'#d2d6da','line-strong':'#737b86',fill:'#eff0ed','fill-hover':'#e5e7e4',sunken:'#edefeb',
      late:'#a32235','late-soft':'#fceef0','late-border':'#ad606d',
      ok:'#256342','ok-soft':'#e9f4ec','ok-border':'#598365',
      warn:'#7a4a08','warn-soft':'#fff4dd','warn-border':'#957034',
      info:'#205782','info-soft':'#eaf2fa','info-border':'#64819d',
      pending:'#525962','pending-soft':'#eff0ed','pending-border':'#737b86',
      'on-accent':'#ffffff','shadow-color':'#20252b0d'},
    dark: {bg:'#141619',surface:'#191c20','surface-2':'#20242a','surface-3':'#292d33',
      'nav-bg':'#191c20',text:'#e7e9ed','text-2':'#c0c7d0','text-3':'#a6afb9',
      line:'#343a43','line-strong':'#808b99',fill:'#252a31','fill-hover':'#30363e',sunken:'#111417',
      late:'#ffadb7','late-soft':'#40252c','late-border':'#b97983',
      ok:'#a3e1b8','ok-soft':'#20372b','ok-border':'#739f83',
      warn:'#f1d29b','warn-soft':'#3a3020','warn-border':'#a38756',
      info:'#a6d2f9','info-soft':'#223344','info-border':'#7095b6',
      pending:'#c0c7d0','pending-soft':'#2c3036','pending-border':'#808b99',
      'on-accent':'#141619','shadow-color':'#00000000'},
  };
  function tint(value, accent, amount) {
    const rgb = s => [1,3,5].map(i=>parseInt(s.slice(i,i+2),16));
    const base=rgb(value), hue=rgb(accent);
    return '#' + base.map((v,i)=>Math.round(v*(1-amount)+hue[i]*amount)
      .toString(16).padStart(2,'0')).join('');
  }
  function tokens(id, mode) {
    const palette=items.find(p=>p.id===id)||items[0], resolved=mode==='dark'?'dark':'light';
    const [accent,hover,soft]=palette[resolved];
    const values={...neutral[resolved],accent,'accent-2':hover,'accent-soft':soft,
      'accent-border':accent,focus:resolved==='light'?accent:hover};
    const surfaces=['bg','surface','surface-2','surface-3','nav-bg','fill','fill-hover','sunken'];
    if(palette.id==='graphite') {
      // All structural colors are achromatic; semantic status colors stay independent.
      for(const key of [...surfaces,'text','text-2','text-3','line','line-strong','on-accent']) {
        const hex=values[key], n=Math.round([1,3,5].reduce((sum,i)=>sum+parseInt(hex.slice(i,i+2),16),0)/3);
        values[key]='#'+n.toString(16).padStart(2,'0').repeat(3);
      }
    } else for(const key of surfaces) values[key]=tint(values[key],accent,0.02);
    return values;
  }
  return {items,tokens};
})();
