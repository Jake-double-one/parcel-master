// ==UserScript==
// @name         Parcel Master: Amazon live tracking
// @namespace    parcel-master
// @version      2
// @description  Passes on to your Parcel Master what Amazon's tracking page shows you: the delivery window and how many stops are left.
// @match        https://www.amazon.de/*progress-tracker/package*
// @match        https://www.amazon.de/*ship-track*
// @grant        GM_xmlhttpRequest
// @connect      __HOST__
// @run-at       document-idle
// ==/UserScript==

// Reads what is written on the page you have open and hands it to your own
// server. It asks Amazon nothing by itself and sends nothing anywhere else.
(function () {
  "use strict";
  const SERVER = "__SERVER__";
  let last = "";

  function text(selector) {
    const element = document.querySelector(selector);
    return element ? element.textContent.replace(/\s+/g, " ").trim() : "";
  }

  function look() {
    const steps = document.querySelectorAll(".pt-status-milestone");
    const map = document.querySelector("#mapTracker");
    const payload = {
      // "Trackingnummer DE6053615519": the server picks the number out.
      tracking: text(".pt-delivery-card-trackingId") || text(".tracking-event-trackingId-text"),
      headline: text(".pt-promise-main-slot"),
      status: text(".pt-status-main-status"),
      // The bubble on the map: "Du bist der nächste Halt", "… Stopps entfernt".
      bubble: text(".mapTracker-da-bubble .H_ib_content"),
      // Whether the map is on the page at all; it can vanish in the middle of a round.
      map: !!(map && map.offsetParent !== null),
      reached: Array.from(steps).filter((step) => step.dataset.reached === "true").length,
      steps: steps.length,
    };
    if (!payload.tracking) return;
    const body = JSON.stringify({ payload });
    if (body === last) return; // nothing new on the page
    last = body;
    GM_xmlhttpRequest({
      method: "POST",
      url: SERVER + "/api/companion/de.amazon",
      headers: { "Content-Type": "application/json" },
      data: body,
      onerror: () => console.warn("Parcel Master: could not reach " + SERVER),
    });
  }

  look();
  setInterval(look, 15000); // the page refreshes its map by itself
})();
