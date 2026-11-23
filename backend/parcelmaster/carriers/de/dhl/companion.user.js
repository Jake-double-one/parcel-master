// ==UserScript==
// @name         Parcel Master: DHL live tracking
// @namespace    parcel-master
// @version      5
// @description  Passes on to your Parcel Master what DHL shows you: the live delivery window, the stops left and the parcels in your account.
// @match        https://www.dhl.de/*/verfolgen.html*
// @match        https://www.dhl.de/*/kundenkonto/*
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      __HOST__
// @run-at       document-start
// ==/UserScript==

// The page asks DHL for the shipment again and again while it is open. This
// script looks at those answers as they arrive and hands them to your own
// server. It asks DHL nothing by itself and sends nothing anywhere else.
// Parcels your Parcel Master does not track yet get a button to take them.
(function () {
  "use strict";
  const SERVER = "__SERVER__";
  const page = typeof unsafeWindow === "undefined" ? window : unsafeWindow;
  const original = page.fetch;
  // Asked when needed: this early the page has not said yet which language it is in.
  function texts() {
    const root = document.documentElement;
    const german = ((root && root.lang) || navigator.language || "").startsWith("de");
    return german
      ? { take: "In Parcel Master verfolgen", taken: "In Parcel Master ✓", wait: "Vorgeschlagen in Parcel Master · jetzt verfolgen" }
      : { take: "Track in Parcel Master", taken: "In Parcel Master ✓", wait: "Suggested in Parcel Master · track now" };
  }
  let last = "";
  // Numbers as the server writes them, so one parcel is one entry however the page spells it.
  const plain = (number) => String(number).replace(/[\s-]/g, "").toUpperCase();
  const pieces = new Map(); // number -> the fullest answer seen for it
  const state = new Map(); // number -> "tracked" | "suggested" | "unknown"

  function send(body, done) {
    GM_xmlhttpRequest({
      method: "POST",
      url: SERVER + "/api/companion/de.dhl",
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify(body),
      onload: (response) => {
        try {
          const answer = JSON.parse(response.responseText);
          for (const number of body.payload.sendungen.map((piece) => plain(piece.id))) {
            state.set(number, "unknown");
          }
          for (const number of answer.suggested || []) state.set(plain(number), "suggested");
          for (const number of answer.tracked || []) state.set(plain(number), "tracked");
          if (done) done();
          paint();
        } catch (error) {
          // Not our server's answer, such as a login page in front of it.
        }
      },
      onerror: () => console.warn("Parcel Master: could not reach " + SERVER),
    });
  }

  function passOn(payload) {
    if (!payload || !Array.isArray(payload.sendungen) || !payload.sendungen.length) return;
    for (const piece of payload.sendungen) {
      if (!piece || typeof piece.id !== "string") continue;
      const known = pieces.get(plain(piece.id));
      const full = piece.sendungsdetails && piece.sendungsdetails.sendungsverlauf;
      if (!known || full) pieces.set(plain(piece.id), piece);
    }
    const body = JSON.stringify(payload);
    if (body === last) return; // nothing new since the last answer
    last = body;
    send({ payload });
  }

  function take(number, button) {
    const piece = pieces.get(number);
    if (!piece) return;
    button.disabled = true;
    send({ payload: { sendungen: [piece] }, take: [number] });
  }

  // One button per shipment the page lists; where the page has no such list,
  // one in the corner for the shipment it shows.
  function paint() {
    const listed = new Set();
    for (const box of document.querySelectorAll("[data-shipment-id]")) {
      const number = plain(box.getAttribute("data-shipment-id"));
      listed.add(number);
      place(number, box.closest("article") || box, false);
    }
    const loose = [...state.keys()].filter((number) => !listed.has(number));
    for (const number of loose) place(number, corner(), true, loose.length > 1);
  }

  // Buttons without a place in the page stack in the corner, one below the other.
  function corner() {
    let box = document.getElementById("parcel-master-corner");
    if (!box) {
      box = document.createElement("div");
      box.id = "parcel-master-corner";
      box.style.cssText =
        "position:fixed;right:16px;bottom:16px;z-index:99999;display:flex;" +
        "flex-direction:column;align-items:flex-end;gap:8px;";
      document.body.appendChild(box);
    }
    return box;
  }

  function place(number, parent, floating, named) {
    const known = state.get(number);
    if (!known) return;
    let button = parent.querySelector(':scope > button[data-parcel-master="' + number + '"]');
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.dataset.parcelMaster = number;
      button.style.cssText =
        "margin:8px 16px 12px;padding:6px 14px;border-radius:999px;border:1px solid #888;" +
        "background:#fff;color:#111;font:inherit;font-size:14px;cursor:pointer;" +
        (floating ? "margin:0;" : "");
      button.title = number;
      button.addEventListener("click", () => take(number, button));
      parent.appendChild(button);
    }
    button.disabled = known === "tracked";
    button.style.opacity = known === "tracked" ? "0.6" : "1";
    const TEXT = texts();
    let label = known === "tracked" ? TEXT.taken : known === "suggested" ? TEXT.wait : TEXT.take;
    if (named) label += " · " + number; // several in the corner: say which is which
    if (button.textContent !== label) button.textContent = label; // no needless redraw
  }

  // The page redraws itself; put the buttons back when it does.
  let pending = 0;
  function watch() {
    new MutationObserver(() => {
      if (pending || !state.size) return;
      pending = setTimeout(() => {
        pending = 0;
        paint();
      }, 500);
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
  if (document.documentElement) watch();
  else document.addEventListener("DOMContentLoaded", watch);

  page.fetch = function (...args) {
    const answer = original.apply(this, args);
    try {
      const url = String(args[0] && args[0].url ? args[0].url : args[0]);
      // Any answer of dhl.de itself that carries shipments; which address the
      // page uses for them differs between tracking and the customer account.
      const own = url.startsWith("/") || new URL(url, location.href).hostname.endsWith("dhl.de");
      if (own) {
        answer
          .then((response) => {
            const kind = response.headers.get("content-type") || "";
            return kind.includes("json") ? response.clone().json() : null;
          })
          .then(passOn)
          .catch(() => {});
      }
    } catch (error) {
      // Never get in the page's way.
    }
    return answer;
  };
})();
