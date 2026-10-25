// Offline Counterform palettes. Color values live here and at :root, never in components.
export const THEMES = Object.freeze([
  { id: "counterform-dark", label: "Counterform Dark", scheme: "dark", bg: "#111316", panel: "#191d22", "panel-2": "#21272d", ink: "#eee8db", muted: "#a7b1b4", accent: "#f08072", "on-accent": "#111316", line: "#364049", bad: "#ff9b8f", ok: "#b5ceb3" },
  { id: "counterform-light", label: "Counterform Light", scheme: "light", bg: "#f7f3ea", panel: "#eee8db", "panel-2": "#ffffff", ink: "#111316", muted: "#546166", accent: "#9f3f33", "on-accent": "#ffffff", line: "#c6c3b9", bad: "#a32f2a", ok: "#3f6948" },
  { id: "graphite", label: "Graphite", scheme: "dark", bg: "#16181b", panel: "#1e2227", "panel-2": "#2a3037", ink: "#eceff4", muted: "#b7c0cc", accent: "#ffb088", "on-accent": "#15171a", line: "#56616e", bad: "#ffb4b4", ok: "#bde0ba" },
  { id: "midnight", label: "Midnight", scheme: "dark", bg: "#0f172a", panel: "#172139", "panel-2": "#22304b", ink: "#eff6ff", muted: "#b6c8e1", accent: "#f4b39f", "on-accent": "#0f172a", line: "#506581", bad: "#ffb4c4", ok: "#b4ddcf" },
  { id: "forest", label: "Forest", scheme: "dark", bg: "#0e1c19", panel: "#182924", "panel-2": "#233c33", ink: "#f0ece3", muted: "#bfd0c3", accent: "#f4b39b", "on-accent": "#0e1c19", line: "#5f7969", bad: "#ffc0b0", ok: "#cde5ad" },
  { id: "sand", label: "Sand", scheme: "light", bg: "#f5eddf", panel: "#eadecd", "panel-2": "#fffaf0", ink: "#292119", muted: "#69533f", accent: "#8b362c", "on-accent": "#fffaf0", line: "#bca68b", bad: "#9c2935", ok: "#31552e" },
  { id: "paper", label: "Paper", scheme: "light", bg: "#fafaf7", panel: "#efefea", "panel-2": "#ffffff", ink: "#202321", muted: "#575e58", accent: "#913d32", "on-accent": "#ffffff", line: "#b7beb7", bad: "#9b233b", ok: "#2e5938" },
  { id: "high-contrast", label: "High Contrast", scheme: "dark", bg: "#000000", panel: "#080808", "panel-2": "#141414", ink: "#ffffff", muted: "#f0f0f0", accent: "#ffff4d", "on-accent": "#000000", line: "#b8b8b8", bad: "#ffbfbf", ok: "#bcffb5" },
].map(Object.freeze));

export function luminance(hex) {
  if (!/^#[a-f0-9]{6}$/i.test(hex)) throw new Error("Invalid palette color");
  const linear = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
  return linear.reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
}
export function contrast(a, b) {
  const [low, high] = [luminance(a), luminance(b)].sort((x, y) => x - y);
  return (high + 0.05) / (low + 0.05);
}
export function resolveTheme(id, dark = true) {
  return THEMES.find((theme) => theme.id === id) || THEMES[dark ? 0 : 1];
}
export function applyTheme(id, root = document.documentElement, dark = matchMedia("(prefers-color-scheme: dark)").matches) {
  const theme = resolveTheme(id, dark);
  for (const [key, value] of Object.entries(theme)) if (key !== "id" && key !== "label" && key !== "scheme")
    root.style.setProperty("--" + key, value);
  root.style.setProperty("--brand-filter", theme.scheme === "light" ? "brightness(0) saturate(100%)" : "none");
  root.style.colorScheme = theme.scheme;
  root.dataset.theme = theme.id;
  root.dataset.themeChoice = id || "system";
  return theme;
}
