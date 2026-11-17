import { api } from "./api";

/**
 * Texts come from the server, which already fills gaps with English.
 * A key that exists nowhere is shown as the key, so it is easy to spot.
 */
let texts = $state<Record<string, string>>({});
let current = $state("en");

export const i18n = {
  get language() {
    return current;
  },
  async load(language: string) {
    texts = await api.texts(language);
    current = language;
    document.documentElement.lang = language;
  },
};

export function t(key: string, params: Record<string, string | number> = {}): string {
  let text = texts[key] ?? key;
  for (const [name, value] of Object.entries(params)) {
    text = text.replaceAll(`{${name}}`, String(value));
  }
  return text;
}

/** Translate a key if it exists; otherwise return the fallback. */
export function tOr(key: string, fallback: string): string {
  return texts[key] ?? fallback;
}

/** The language to start with before the server told us its setting. */
export function browserLanguage(available: string[]): string {
  for (const wanted of navigator.languages ?? [navigator.language]) {
    const short = wanted.toLowerCase().split("-")[0];
    if (available.includes(short)) return short;
  }
  return "en";
}
