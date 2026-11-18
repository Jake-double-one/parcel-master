// ==UserScript==
// @name         Parcel Master: DHL live tracking
// @namespace    parcel-master
// @version      2
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
(function () {
  "use strict";
  const SERVER = "__SERVER__";
  const page = typeof unsafeWindow === "undefined" ? window : unsafeWindow;
  const original = page.fetch;
  let last = "";

  function passOn(payload) {
    if (!payload || !Array.isArray(payload.sendungen) || !payload.sendungen.length) return;
    const body = JSON.stringify({ payload });
    if (body === last) return; // nothing new since the last answer
    last = body;
    GM_xmlhttpRequest({
      method: "POST",
      url: SERVER + "/api/companion/de.dhl",
      headers: { "Content-Type": "application/json" },
      data: body,
      onerror: () => console.warn("Parcel Master: could not reach " + SERVER),
    });
  }

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
