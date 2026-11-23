// ==UserScript==
// @name         Parcel Master: DHL live tracking
// @namespace    parcel-master
// @version      10
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
  const settled = new Set(); // arrived or gone back: nothing left to take

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
      const details = piece.sendungsdetails || {};
      if (full && (details.istZugestellt || details.ruecksendung)) settled.add(plain(piece.id));
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

  // One button on the card of each shipment the page lists.
  function paint() {
    for (const box of document.querySelectorAll("[data-shipment-id]")) {
      const number = plain(box.getAttribute("data-shipment-id"));
      if (ready(number, box)) place(number, box);
    }
    // A parcel that arrived needs no button, unless it shows that we have it.
    for (const button of document.querySelectorAll("button[data-parcel-master]")) {
      const number = button.dataset.parcelMaster;
      const card = button.parentElement && button.parentElement.hasAttribute("data-shipment-id");
      const wanted = state.get(number) === "tracked" || !settled.has(number);
      if (!wanted || !card || !ready(number, button.parentElement)) button.remove();
    }
  }

  // The page draws a card first and fills in the status later. Until then
  // nobody knows whether the parcel has arrived, so the button waits.
  function ready(number, card) {
    const piece = pieces.get(number);
    const told = !!(piece && piece.sendungsdetails && piece.sendungsdetails.sendungsverlauf);
    return told || !!card.querySelector('[data-role="shipment-status-text"]');
  }

  function place(number, card) {
    const known = state.get(number);
    if (!known) return;
    if (known !== "tracked" && settled.has(number)) return;
    let button = card.querySelector(':scope > button[data-parcel-master="' + number + '"]');
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.dataset.parcelMaster = number;
      button.title = number;
      button.addEventListener("click", () => take(number, button));
    }
    if (card.lastElementChild !== button) card.appendChild(button);
    // In line with the card's heading, whatever stands left of it.
    const heading = card.querySelector("h3");
    const inset = heading
      ? Math.round(heading.getBoundingClientRect().left - card.getBoundingClientRect().left)
      : 16;
    const tracked = known === "tracked";
    // Ours: filled while there is something to press; once tracked only an
    // outline in the card's own text colour, which reads on light and on dark.
    const style =
      "display:block;width:fit-content;align-self:flex-start;margin:4px 16px 16px " + Math.max(inset, 16) + "px;padding:7px 16px;" +
      "border-radius:999px;border:1px solid #6f68d8;font:inherit;font-size:14px;font-weight:700;" +
      (tracked
        ? "background:transparent;color:inherit;cursor:default;"
        : "background:#4b45a8;color:#fff;cursor:pointer;");
    if (button.dataset.look !== style) {
      button.style.cssText = style;
      button.dataset.look = style;
    }
    button.disabled = tracked;
    const TEXT = texts();
    const label = tracked ? TEXT.taken : known === "suggested" ? TEXT.wait : TEXT.take;
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

  // -- pressing "Status aktualisieren" for the user ------------------------
  // The live view only moves when its button is pressed. While it is open the
  // script presses it at a calm pace, faster as the van comes near, and shows
  // the seconds left next to it. A click on that note pauses and resumes.
  const REFRESH = /status aktualisieren|update status|refresh status/i;
  let left = 0; // seconds until the next press
  let paused = false;

  function refreshButton() {
    return [...document.querySelectorAll("button")].find(
      (button) => REFRESH.test(button.textContent || "") && !button.disabled,
    );
  }

  function pace(button) {
    const area = button.closest("section, article, div[class]") || document.body;
    const words = (area.parentElement || area).textContent || "";
    const found = /(\d+)\s*(Zustellstopps?|stops?)/i.exec(words);
    const stops = found ? Number(found[1]) : null;
    if (stops !== null && stops < 5) return 30;
    if (stops !== null && stops < 10) return 60;
    return 120;
  }

  function note(button) {
    let label = button.parentElement.querySelector("[data-parcel-master-refresh]");
    if (!label) {
      label = document.createElement("button");
      label.type = "button";
      label.dataset.parcelMasterRefresh = "1";
      label.style.cssText =
        "margin-left:12px;padding:6px 12px;border-radius:999px;border:1px solid #888;" +
        "background:#fff;color:#111;font:inherit;font-size:13px;cursor:pointer;";
      label.addEventListener("click", () => {
        paused = !paused;
        tick();
      });
      button.insertAdjacentElement("afterend", label);
    }
    const german = texts().take.includes("verfolgen");
    const text = paused
      ? german ? "Parcel Master: pausiert · weiter" : "Parcel Master: paused · resume"
      : german ? "Parcel Master drückt in " + left + " s" : "Parcel Master presses in " + left + " s";
    if (label.textContent !== text) label.textContent = text;
  }

  function tick() {
    const button = refreshButton();
    if (!button) {
      left = 0; // no live view open: start afresh when one appears
      return;
    }
    if (left <= 0) left = pace(button);
    if (!paused) {
      left -= 1;
      if (left <= 0) {
        button.click();
        left = pace(button);
      }
    }
    note(button);
  }

  // A worker keeps time in a tab that is not in front, where the page's own
  // timers are slowed down; if the page does not allow one, a plain timer does.
  function startClock() {
    try {
      const code = "setInterval(() => postMessage(0), 1000);";
      const worker = new Worker(URL.createObjectURL(new Blob([code], { type: "text/javascript" })));
      worker.onmessage = tick;
      worker.onerror = () => {
        worker.terminate();
        setInterval(tick, 1000);
      };
    } catch (error) {
      setInterval(tick, 1000);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startClock);
  else startClock();

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
