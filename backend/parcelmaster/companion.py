"""One userscript out of the companion scripts of all carrier modules.

Each module keeps its own script, complete with a userscript header, so it
can be read and tried on its own. For the user's browser they are put
together: one script to install, and one address the script manager asks
for updates.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .carriers.registry import Registry

#: Where the combined script is served, and where it looks for its updates.
PATH = "/api/companion/parcel-master.user.js"
#: Raised when the way the parts are put together changes.
BUILD = 2
_HEADER = re.compile(r"// ==UserScript==\n(.*?)// ==/UserScript==\n", re.S)
_LINE = re.compile(r"^// @(\S+)\s+(.*?)\s*$", re.M)
#: What may stand in the address a script is made out to; anything else could
#: break out of the script text.
_SERVER = re.compile(r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?")


def safe_server(server: str) -> str | None:
    """The server address as it may be written into a script, or None."""
    server = server.rstrip("/")
    return server if _SERVER.fullmatch(server) and urlparse(server).hostname else None


def _split(script: str) -> tuple[dict[str, list[str]], str]:
    """A userscript's header fields and the code after the header."""
    header = _HEADER.search(script)
    if header is None:
        return {}, script
    fields: dict[str, list[str]] = {}
    for key, value in _LINE.findall(header.group(1)):
        fields.setdefault(key, []).append(value)
    return fields, script[header.end() :]


def version(registry: Registry) -> str | None:
    """The version the combined script carries, e.g. "1.11"; None without any script."""
    script = combined(registry)
    found = re.search(r"^// @version\s+(\S+)", script or "", re.M)
    return found.group(1) if found else None


def combined(registry: Registry) -> str | None:
    """All companion scripts as one, with ``__SERVER__`` and ``__HOST__`` left to fill in."""
    parts = []
    for module in sorted(registry.all(), key=lambda m: m.id):
        script = module.companion_script()
        if script:
            parts.append((module, *_split(script)))
    if not parts:
        return None
    matches = [match for _, fields, _ in parts for match in fields.get("match", [])]
    grants = sorted({grant for _, fields, _ in parts for grant in fields.get("grant", [])})
    # Any part that changes raises the sum, so the script manager sees an update.
    version = sum(int(fields.get("version", ["0"])[0]) for _, fields, _ in parts)
    names = ", ".join(module.name for module, _, _ in parts)
    header = [
        "// ==UserScript==",
        "// @name         Parcel Master",
        "// @namespace    parcel-master",
        f"// @version      {BUILD}.{version}",
        f"// @description  Connects the pages of {names} with your Parcel Master: passes on "
        "what they show you about your parcels and adds buttons to take a shipment.",
        *(f"// @match        {match}" for match in matches),
        *(f"// @grant        {grant}" for grant in grants),
        "// @connect      __HOST__",
        f"// @updateURL    __SERVER__{PATH}",
        f"// @downloadURL  __SERVER__{PATH}",
        "// @run-at       document-start",
        "// ==/UserScript==",
        "",
        "// Put together by your Parcel Master from the scripts of its carrier modules.",
        "// Each part only runs on its own carrier's pages and sends only to your server.",
        "function parcelMasterWhenReady(run) {",
        '  if (document.readyState !== "loading") run();',
        '  else document.addEventListener("DOMContentLoaded", run);',
        "}",
        "",
    ]
    body = []
    for module, fields, code in parts:
        hosts = sorted(
            {urlparse(match.replace("*", "x")).hostname or "" for match in fields.get("match", [])}
        )
        late = fields.get("run-at", ["document-start"])[0] != "document-start"
        body.append(f"// ---- {module.name} ({module.id}) " + "-" * 40)
        body.append(f"if ({hosts!r}.includes(location.hostname)) {{".replace("'", '"'))
        if late:
            body.append("parcelMasterWhenReady(function () {")
        body.append(code.strip())
        if late:
            body.append("});")
        body.append("}")
        body.append("")
    return "\n".join(header + body)


def made_out(script: str, server: str) -> str:
    return script.replace("__SERVER__", server).replace("__HOST__", urlparse(server).hostname or "")
