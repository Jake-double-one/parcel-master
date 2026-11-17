"""Translations. One JSON file per language; English fills every gap.

Files live in ``parcelmaster/locales`` and in each carrier module. A file
dropped into ``<data dir>/locales`` adds a language or overrides single texts
without rebuilding anything.
"""

from __future__ import annotations

import json
from pathlib import Path

FALLBACK = "en"
BUILTIN_DIR = Path(__file__).parent / "locales"


def _flatten(prefix: str, node: object, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten(f"{prefix}.{key}" if prefix else key, value, out)
    elif isinstance(node, str):
        out[prefix] = node


class Translations:
    def __init__(
        self, module_texts: dict[str, dict[str, str]], extra_dir: Path | None = None
    ) -> None:
        self._texts: dict[str, dict[str, str]] = {}
        for directory in (BUILTIN_DIR, extra_dir):
            if directory is None or not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                flat: dict[str, str] = {}
                _flatten("", json.loads(path.read_text("utf-8")), flat)
                self._texts.setdefault(path.stem, {}).update(flat)
        for language, texts in module_texts.items():
            self._texts.setdefault(language, {}).update(texts)
        if FALLBACK not in self._texts:
            raise RuntimeError("the English locale file is missing")

    def languages(self) -> list[dict[str, str]]:
        return [
            {"code": code, "name": texts.get("meta.name", code)}
            for code, texts in sorted(self._texts.items())
            if "meta.name" in texts
        ]

    def resolve(self, language: str) -> str:
        return language if language in self._texts else FALLBACK

    def catalog(self, language: str) -> dict[str, str]:
        """Every text for ``language``, with English filling what is missing."""
        return {**self._texts[FALLBACK], **self._texts.get(language, {})}

    def exact(self, language: str, key: str) -> str | None:
        """The text for ``key`` in exactly this language, without the English fallback."""
        return self._texts.get(language, {}).get(key)

    def localize(self, module: object, text: str, language: str) -> str:
        """A carrier's status text in ``language``, as far as its module can translate it."""
        if not text or language == FALLBACK:
            return text
        prefix = f"carrier.{module.id}."  # type: ignore[attr-defined]
        return module.localize(text, lambda key: self.exact(language, prefix + key))  # type: ignore[attr-defined]

    def t(self, language: str, key: str, **params: object) -> str:
        text = self._texts.get(language, {}).get(key) or self._texts[FALLBACK].get(key) or key
        for name, value in params.items():
            text = text.replace("{" + name + "}", str(value))
        return text
