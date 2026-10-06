/* Device preferences affect presentation only, never approval or connector authority. */
const UIPreferences = (() => {
  const defaults = { theme: "auto", density: "comfortable", motion: "auto", home: "campaigns" };
  let saved = {};
  try { saved = JSON.parse(window.localStorage?.getItem("onx-ui-preferences-v1") || "{}"); } catch { }
  let current = { ...defaults, ...saved };
  function apply() {
    const root = document.documentElement;
    if (!root) return;
    root.dataset.theme = ["auto", "light", "dark"].includes(current.theme) ? current.theme : "auto";
    root.dataset.density = current.density === "compact" ? "compact" : "comfortable";
    root.dataset.motion = current.motion === "reduced" ? "reduced" : "auto";
  }
  function save(values) {
    current = { ...defaults, ...values };
    try { window.localStorage?.setItem("onx-ui-preferences-v1", JSON.stringify(current)); } catch { }
    apply();
  }
  apply();
  return { get: () => ({ ...current }), save };
})();
