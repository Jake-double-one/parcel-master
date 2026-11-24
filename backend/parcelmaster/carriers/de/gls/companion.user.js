// ==UserScript==
// @name         Parcel Master: GLS
// @namespace    parcel-master
// @version      1
// @description  Passes the history GLS' tracking page shows you on to your Parcel Master and adds a button that takes the parcel.
// @match        https://www.gls-pakete.de/*
// @grant        GM_xmlhttpRequest
// @connect      __HOST__
// @run-at       document-idle
// ==/UserScript==

// GLS shows a parcel's history only on this page. This script reads it from
// what the page displays and hands it to your own server: the parcel number,
// the steps and the sender's name. It asks GLS nothing by itself, leaves the
// recipient out and sends nothing anywhere else.
(function () {
  "use strict";
  const SERVER = "__SERVER__";
  const german = (document.documentElement.lang || navigator.language || "").startsWith("de");
  const TEXT = german
    ? { take: "In Parcel Master importieren", taken: "In Parcel Master ✓", wait: "Vorgeschlagen in Parcel Master · jetzt importieren" }
    : { take: "Import into Parcel Master", taken: "In Parcel Master ✓", wait: "Suggested in Parcel Master · import now" };
  let parcel = null; // what the page shows right now
  let known = ""; // "tracked" | "suggested" | "unknown"
  let last = "";

  function text(node) {
    return node ? node.textContent.replace(/\s+/g, " ").trim() : "";
  }

  // The parcel as the page shows it, or null while it is not there yet.
  function read() {
    const shown = text(document.querySelector('[data-testid="tracking-number"] [class*="infoName"]'));
    const query = new URLSearchParams(location.search);
    const number = (shown || query.get("trackingNumber") || query.get("match") || "").replace(/\D/g, "");
    const list = document.querySelector('[data-testid="event-container"]');
    if (!/^\d{11,12}$/.test(number) || !list) return null;
    const events = [];
    for (const time of list.querySelectorAll("time[datetime]")) {
      const block = time.parentElement.parentElement;
      const words = block && (block.querySelector('[class*="eventDescription"]') || block.children[1]);
      const place = time.parentElement.querySelector('[data-testid="event-location"]');
      if (!words) continue;
      events.push({
        time: time.getAttribute("datetime"),
        text: text(words),
        place: text(place).replace(/^\|\s*/, ""),
      });
    }
    const sender = document.querySelector('[data-testid="sender-information-label"] [class*="infoBlock"] span');
    return { number: number, sender: text(sender), events: events };
  }

  function send(take) {
    if (!parcel) return;
    const number = parcel.number;
    GM_xmlhttpRequest({
      method: "POST",
      url: SERVER + "/api/companion/de.gls",
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ payload: { parcels: [parcel] }, take: take ? [number] : [] }),
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

  // The button sits above the history; in the corner if the page has none.
  function paint() {
    if (!known) return;
    let button = document.querySelector("button[data-parcel-master]");
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.dataset.parcelMaster = "1";
      button.addEventListener("click", (event) => {
        if (!event.isTrusted) return; // only a person takes a parcel
        button.disabled = true;
        send(true);
      });
    }
    const list = document.querySelector('[data-testid="event-container"]');
    const heading = list && list.parentElement.querySelector('[class*="historyHeading"]');
    const anchor = heading || list;
    const look =
      "display:block;width:fit-content;margin:16px 0;padding:10px 18px;border:0;border-radius:999px;" +
      "background:#4b45a8;color:#fff;font:inherit;font-weight:700;" +
      (anchor ? "" : "position:fixed;right:16px;bottom:16px;z-index:99999;") +
      (known === "tracked" ? "opacity:0.6;cursor:default;" : "cursor:pointer;");
    if (button.dataset.look !== look) {
      button.dataset.look = look; // no needless redraw
      button.style.cssText = look;
    }
    if (anchor) {
      if (anchor.previousElementSibling !== button) anchor.insertAdjacentElement("beforebegin", button);
    } else if (button.parentElement !== document.body) {
      document.body.appendChild(button);
    }
    button.disabled = known === "tracked";
    const label = known === "tracked" ? TEXT.taken : known === "suggested" ? TEXT.wait : TEXT.take;
    if (button.textContent !== label) button.textContent = label;
  }

  function look() {
    const found = read();
    if (found) {
      const body = JSON.stringify(found);
      if (body !== last) {
        last = body; // only what is new is passed on
        parcel = found;
        send(false);
      }
    }
    paint();
  }

  // The page fills in after loading and redraws itself; look again when it does.
  let pending = 0;
  new MutationObserver(() => {
    if (pending) return;
    pending = setTimeout(() => {
      pending = 0;
      look();
    }, 700);
  }).observe(document.documentElement, { childList: true, subtree: true });
  look();
})();
