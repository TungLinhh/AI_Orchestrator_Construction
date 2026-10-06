/* Curated semantic colors. Contrast is checked by verify_console_browser.py. */
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
      light: ["#495566", "#343f50", "#edf0f4"],
      dark: ["#bccad9", "#dce5ee", "#2d3947"],
    },
  ];
  const neutral = {
    light: {
      bg: "#f4f6f9",
      surface: "#ffffff",
      "surface-2": "#f8fafc",
      "nav-bg": "#eef2f6",
      text: "#1b293b",
      "text-2": "#506176",
      "text-3": "#5b6a7d",
      line: "#dbe2eb",
      "line-strong": "#a5b3c5",
      fill: "#edf1f6",
      "fill-hover": "#e3eaf2",
      sunken: "#f0f3f8",
      late: "#a52236",
      "late-soft": "#fff0f2",
      ok: "#246746",
      "ok-soft": "#e9f5ed",
      warn: "#86520e",
      "warn-soft": "#fff5df",
      "on-accent": "#ffffff",
    },
    dark: {
      bg: "#111821",
      surface: "#19232f",
      "surface-2": "#1f2b39",
      "nav-bg": "#151e29",
      text: "#edf2f8",
      "text-2": "#bfccd9",
      "text-3": "#9eafc3",
      line: "#334354",
      "line-strong": "#5d738b",
      fill: "#253343",
      "fill-hover": "#304256",
      sunken: "#131d28",
      late: "#ffa0aa",
      "late-soft": "#432831",
      ok: "#9cddb5",
      "ok-soft": "#243d31",
      warn: "#efcc8e",
      "warn-soft": "#403624",
      "on-accent": "#111821",
    },
  };
  function tokens(id, mode) {
    const palette = items.find((p) => p.id === id) || items[0];
    const resolved = mode === "dark" ? "dark" : "light";
    const [accent, hover, soft] = palette[resolved];
    return {
      ...neutral[resolved],
      accent,
      "accent-2": hover,
      "accent-soft": soft,
    };
  }
  return { items, tokens };
})();
