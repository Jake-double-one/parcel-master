import skinCss from "../styles/skins.css?raw";

export interface Skin {
  id: string;
  name: string;
  nameDe: string;
  scheme: "light" | "dark";
  isDefault: boolean;
  order: number;
  body: string;
}

/**
 * Skins describe themselves in a comment above their block (name, scheme,
 * default, order). Adding a skin to skins.css is enough to list it here.
 */
export function parseSkins(css: string): Skin[] {
  const skins: Skin[] = [];
  for (const match of css.matchAll(/\/\*([\s\S]*?)\*\/\s*([^{]*)\{([^}]*)\}/g)) {
    const meta: Record<string, string> = {};
    for (const line of match[1].split("\n")) {
      const tag = line.match(/^\s*\*?\s*([a-z]+(?:\.[a-z-]+)?)\s*:\s*(.+?)\s*$/i);
      if (tag) meta[tag[1].toLowerCase()] = tag[2];
    }
    const id = match[2].match(/data-skin="([a-z0-9-]+)"/)?.[1];
    if (!id) continue;
    const scheme = meta.scheme === "dark" ? "dark" : "light";
    skins.push({
      id,
      name: meta.name ?? id,
      nameDe: meta["name.de"] ?? meta.name ?? id,
      scheme,
      isDefault: meta.default === scheme,
      order: Number(meta.order) || 100,
      body: match[3],
    });
  }
  return skins.sort((a, b) => a.order - b.order);
}

export const SKINS = parseSkins(skinCss);

function standard(scheme: "light" | "dark"): Skin {
  return (
    SKINS.find((s) => s.scheme === scheme && s.isDefault) ??
    SKINS.find((s) => s.scheme === scheme) ??
    SKINS[0]
  );
}

/** Without a choice the page follows the browser: its light or dark default skin. */
export function installAutoSkin(): void {
  const style = document.createElement("style");
  style.textContent =
    `:root[data-theme="auto"] {${standard("light").body}}\n` +
    `@media (prefers-color-scheme: dark) {\n  :root[data-theme="auto"] {${standard("dark").body}}\n}`;
  document.head.append(style);
}

export function pickedSkin(): string | null {
  try {
    return localStorage.getItem("pm.skin");
  } catch {
    return null;
  }
}

export function currentSkin(): string {
  const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  return pickedSkin() ?? standard(dark ? "dark" : "light").id;
}

export function setSkin(id: string): void {
  document.documentElement.dataset.theme = id;
  try {
    localStorage.setItem("pm.skin", id);
  } catch {
    // Private windows may refuse storage; the skin still applies for this visit.
  }
  syncThemeColor();
}

/** Keep the browser chrome in the colour of the page background. */
export function syncThemeColor(): void {
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  if (bg) document.querySelector('meta[name="theme-color"]')?.setAttribute("content", bg);
}
