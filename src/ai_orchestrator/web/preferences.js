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
  const system = window.matchMedia?.("(prefers-color-scheme: dark)");
  function apply() {
    const root = document.documentElement;
    if (!root) return;
    const resolved =
      current.theme === "auto"
        ? system?.matches
          ? "dark"
          : "light"
        : current.theme;
    Object.assign(root.dataset, current, { colorMode: resolved });
    root.style.colorScheme = resolved;
    for (const [key, value] of Object.entries(
      UIPalettes.tokens(current.palette, resolved),
    ))
      root.style.setProperty("--" + key, value);
  }
  function save(values) {
    current = normalize(values);
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
  return {
    get: () => ({ ...current }),
    save,
    preview: (values) => {
      current = normalize(values);
      apply();
    },
  };
})();
