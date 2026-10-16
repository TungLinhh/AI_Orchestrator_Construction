/* Device preferences affect presentation only, never approval or connector authority. */
const UIPreferences = (() => {
  const defaults = {
    theme: "auto",
    palette: "navy",
    density: "comfortable",
    font: "inter",
    motion: "auto",
    home: "campaigns",
  };
  /* Body font choices: small set of locally available stacks, each with
     Vietnamese coverage through OS fonts or the bundled Inter subset.
     Code/monospace stays on JetBrains Mono deliberately and is not a choice.
     Unknown stored values fall back to the default without touching the
     other stored keys. */
  const fonts = [
    {
      id: "inter",
      en: "Inter (Default)",
      vi: "Inter (Mặc định)",
      stack: '"Inter", "Inter Fallback", Arial, system-ui, sans-serif',
    },
    {
      id: "system",
      en: "System",
      vi: "Hệ thống",
      stack: 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Liberation Sans", sans-serif',
    },
    {
      id: "serif",
      en: "Serif",
      vi: "Serif (Có chân)",
      stack: 'Georgia, "Times New Roman", "Noto Serif", "DejaVu Serif", serif',
    },
  ];
  const choices = {
    theme: ["auto", "light", "dark"],
    palette: UIPalettes.items.map((p) => p.id),
    density: ["comfortable", "compact"],
    font: fonts.map((f) => f.id),
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
    fonts,
    get: () => ({ ...current }),
    save,
    preview: (values) => {
      current = normalize(values);
      apply();
    },
  };
})();
