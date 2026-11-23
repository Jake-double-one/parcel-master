// ==UserScript==
// @name         Parcel Master: Hermes
// @namespace    parcel-master
// @version      1
// @description  Adds a button to Hermes' tracking page that takes the shipment into your Parcel Master, and passes on what the page shows you about it.
// @match        https://www.myhermes.de/empfangen/sendungsverfolgung/*
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      __HOST__
// @run-at       document-start
// ==/UserScript==

// The page asks Hermes for the shipment you look at. This script reads that
// answer as it arrives and hands it to your own server. It asks Hermes nothing
// by itself and sends nothing anywhere else.
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
      ? { take: "In Parcel Master importieren", taken: "In Parcel Master ✓", wait: "Vorgeschlagen in Parcel Master · jetzt importieren" }
      : { take: "Import into Parcel Master", taken: "In Parcel Master ✓", wait: "Suggested in Parcel Master · import now" };
  }
  const ICON =
    '<svg width="24" height="24" viewBox="96 96 320 320" fill="none" stroke-linejoin="round" ' +
    'stroke-linecap="round" stroke-width="30" aria-hidden="true">' +
    '<path d="M256 120 392 192v136L256 400 120 328V192z" stroke="#fff"/>' +
    '<path d="M120 192l136 72 136-72M256 264v136" stroke="#fff"/>' +
    '<path d="M188 156l136 72" stroke="#f0b429"/></svg>';
  let shipment = null; // what Hermes answered for the shipment on this page
  let known = ""; // "tracked" | "suggested" | "unknown"
  let last = "";

  function send(take) {
    if (!shipment) return;
    const number = String(shipment.barcode);
    GM_xmlhttpRequest({
      method: "POST",
      url: SERVER + "/api/companion/de.hermes",
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ payload: { shipments: [shipment] }, take: take ? [number] : [] }),
      onload: (response) => {
        try {
          const answer = JSON.parse(response.responseText);
          known = (answer.tracked || []).includes(number)
            ? "tracked"
            : (answer.suggested || []).includes(number)
              ? "suggested"
              : "unknown";
          paint();
        } catch (error) {
          // Not our server's answer, such as a login page in front of it.
        }
      },
      onerror: () => console.warn("Parcel Master: could not reach " + SERVER),
    });
  }

  function passOn(payload) {
    const found = Array.isArray(payload) ? payload[0] : null;
    if (!found || typeof found.barcode !== "string") return;
    const body = JSON.stringify(found);
    if (body === last) return; // nothing new since the last answer
    last = body;
    shipment = found;
    send(false);
  }

  // The button sits right under the status line; in the corner if the page has none.
  function paint() {
    if (!known) return;
    let button = document.querySelector("button[data-parcel-master]");
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.dataset.parcelMaster = "1";
      button.addEventListener("click", (event) => {
        if (!event.isTrusted) return; // only a person takes a shipment
        button.disabled = true;
        send(true);
      });
    }
    const anchor = document.querySelector(".c-sv2-alert");
    const fixed = "position:fixed;right:16px;bottom:16px;z-index:99999;width:auto;";
    button.style.cssText =
      "display:flex;align-items:center;gap:14px;width:100%;margin:14px 0 18px;padding:14px 18px;" +
      "border:0;border-radius:10px;background:#4b45a8;color:#fff;font:inherit;font-size:18px;" +
      "font-weight:800;text-align:left;cursor:pointer;" +
      (anchor ? "" : fixed);
    if (anchor) {
      if (anchor.nextElementSibling !== button) anchor.insertAdjacentElement("afterend", button);
    } else if (button.parentElement !== document.body) {
      document.body.appendChild(button);
    }
    button.disabled = known === "tracked";
    button.style.opacity = known === "tracked" ? "0.6" : "1";
    button.style.cursor = known === "tracked" ? "default" : "pointer";
    const TEXT = texts();
    const label = known === "tracked" ? TEXT.taken : known === "suggested" ? TEXT.wait : TEXT.take;
    if (button.dataset.label !== label) {
      button.dataset.label = label; // no needless redraw
      button.innerHTML = ICON;
      button.appendChild(document.createElement("span")).textContent = label;
    }
  }

  // The page redraws itself; put the button back when it does.
  let pending = 0;
  function watch() {
    new MutationObserver(() => {
      if (pending || !known) return;
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
      const url = new URL(String(args[0] && args[0].url ? args[0].url : args[0]), location.href);
      // Only Hermes' own answer to the shipment search.
      if (url.hostname === "api.my-deliveries.de" && url.pathname.includes("/shipments/search/")) {
        answer
          .then((response) => (response.ok ? response.clone().json() : null))
          .then(passOn)
          .catch(() => {});
      }
    } catch (error) {
      // Never get in the page's way.
    }
    return answer;
  };
})();
