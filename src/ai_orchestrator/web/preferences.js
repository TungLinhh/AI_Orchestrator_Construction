/* Device preferences affect presentation only, never approval or connector authority. */
const UIPreferences = (() => {
  const defaults = {
    theme: "auto",
    palette: "navy",
    density: "comfortable",
    motion: "auto",
    home: "campaigns",
  };
  const choices = {
    theme: ["auto", "light", "dark"],
    palette: UIPalettes.items.map((p) => p.id),
    density: ["comfortable", "compact"],
    motion: ["auto", "reduced"],
    home: ["campaigns", "organization"],
  };
  const normalize = (values) =>
    Object.fromEntries(
      Object.entries(defaults).map(([key, value]) => [
        key,
        choices[key].includes(values?.[key]) ? values[key] : value,
      ]),
    );
  let saved = {};
  try {
    saved = JSON.parse(
      window.localStorage?.getItem("onx-ui-preferences-v1") || "{}",
    );
  } catch {}
  let current = normalize(saved);
  let persisted = {...current};
  const system = window.matchMedia?.("(prefers-color-scheme: dark)");
  let revision = 0;
  const nextFrame = callback => window.requestAnimationFrame
    ? window.requestAnimationFrame(callback) : setTimeout(callback, 0);
  function ready() {
    nextFrame(()=>nextFrame(()=>{ delete document.documentElement.dataset.uiInitializing; }));
  }
  const root = document.documentElement;
  if(root) {
    root.dataset.uiInitializing='true';
    try {
      const language=window.localStorage?.getItem('ao-lang-v1');
      root.lang=language==='en'?'en':'vi';
      if(window.localStorage?.getItem('ao-sidebar-collapsed')==='true')
        root.classList.add('sidebar-collapsed');
    } catch { root.lang='vi'; }
  }
  function apply() {
    if (!root) return;
    const ticket=++revision;
    root.dataset.uiSwitching='true';
    const resolved=current.theme==='auto'?(system?.matches?'dark':'light'):current.theme;
    Object.assign(root.dataset,current,{colorMode:resolved});
    root.style.colorScheme=resolved;
    const values=UIPalettes.tokens(current.palette,resolved);
    for(const [key,value] of Object.entries(values))
      root.style.setProperty('--primitive-'+key,value);
    const meta=document.getElementById('themeColor');
    if(meta) meta.setAttribute('content',values.bg);
    nextFrame(()=>nextFrame(()=>{if(ticket===revision) delete root.dataset.uiSwitching;}));
  }
  document.addEventListener('DOMContentLoaded',ready,{once:true});
  function save(values) {
    current = normalize(values);
    persisted = {...current};
    try {
      window.localStorage?.setItem(
        "onx-ui-preferences-v1",
        JSON.stringify(current),
      );
    } catch {}
    apply();
  }
  system?.addEventListener("change", apply);
  apply();
  const resetPreview=()=>{current={...persisted};apply();};
  window.addEventListener("hashchange",resetPreview);
  return {
    resetPreview,
    ready,
    get: () => ({ ...current }),
    save,
    preview: (values) => {
      current = normalize(values);
      apply();
    },
  };
})();
