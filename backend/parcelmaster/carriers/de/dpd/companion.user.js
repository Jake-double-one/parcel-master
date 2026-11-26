// ==UserScript==
// @name         Parcel Master: DPD
// @namespace    parcel-master
// @version      1
// @description  Passes what DPD's parcel page shows you on to your Parcel Master and adds a button that takes the parcel.
// @match        https://my.dpd.de/*
// @grant        GM_xmlhttpRequest
// @connect      __HOST__
// @run-at       document-idle
// ==/UserScript==

// DPD shows a parcel's state and, on the delivery day, the van on a map on this
// page. This script reads from what the page displays and hands it to your own
// server: the parcel number, the sender's name, the state in DPD's words, when
// it was delivered, and where the van and the destination are while the van is
// on its way. It asks DPD nothing by itself, leaves the recipient's name and
// address out and sends nothing anywhere else.
(function () {
  "use strict";
  const SERVER = "__SERVER__";
  const PREFIX = "ContentPlaceHolder1_";
  const german = (document.documentElement.lang || navigator.language || "").startsWith("de");
  const TEXT = german
    ? { take: "In Parcel Master importieren", taken: "In Parcel Master ✓", wait: "Vorgeschlagen in Parcel Master · jetzt importieren" }
    : { take: "Import into Parcel Master", taken: "In Parcel Master ✓", wait: "Suggested in Parcel Master · import now" };
  let parcel = null; // what the page shows right now
  let known = ""; // "tracked" | "suggested" | "unknown"
  let last = "";

  function part(name) {
    return document.getElementById(PREFIX + name);
  }

  function text(node) {
    return node ? node.textContent.replace(/\s+/g, " ").trim() : "";
  }

  // A place the page keeps in two hidden fields; "0" stands for "none".
  function spot(name) {
    const lat = Number((part("hidPos" + name + "Lat") || {}).value);
    const lng = Number((part("hidPos" + name + "Lng") || {}).value);
    return Number.isFinite(lat) && Number.isFinite(lng) && (lat !== 0 || lng !== 0) ? [lat, lng] : null;
  }

  // The parcel as the page shows it, or null where it shows none.
  function read() {
    const number = ((part("hidParcelNo") || {}).value || text(part("labParcelNo"))).replace(/\s/g, "");
    if (!/^\d{14}$/.test(number)) return null;
    // The driver's note: its first line is the news, the lines after it name
    // who took the parcel and when. Only the first line and the time are read.
    const note = part("labParcelMessageRating");
    const first = note && note.firstChild && note.firstChild.nodeType === Node.TEXT_NODE
      ? note.firstChild.textContent.replace(/\s+/g, " ").trim()
      : "";
    const when = /\b(\d{1,2})\.(\d{1,2})\.(\d{4}),?\s+(\d{1,2}):(\d{2})\b/.exec(text(note));
    const stage = /status_(\d+)\./.exec((part("imgDeliverStatus") || {}).src || "");
    const van = spot("Car");
    // Which parts the page shows, by name only: tells what else there is to read.
    const box = part("panParcelInfo");
    const parts = box
      ? [...box.querySelectorAll('[id^="' + PREFIX + '"]')].map((node) => node.id.slice(PREFIX.length)).slice(0, 60)
      : [];
    return {
      number: number,
      sender: text(part("labParcelName")),
      status: text(part("labDeliveryStatus")),
      stage: stage ? Number(stage[1]) : null,
      note: first,
      when: when ? when[3] + "-" + when[2].padStart(2, "0") + "-" + when[1].padStart(2, "0") + "T" + when[4].padStart(2, "0") + ":" + when[5] : "",
      van: van,
      home: van ? spot("House") : null, // only of use next to the van
      parts: parts,
    };
  }

  function send(take) {
    if (!parcel) return;
    const number = parcel.number;
    GM_xmlhttpRequest({
      method: "POST",
      url: SERVER + "/api/companion/de.dpd",
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

  // The button sits above the parcel's box; in the corner if the page has none.
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
    const anchor = part("panParcelInfo");
    const look =
      "display:block;width:fit-content;margin:12px 0;padding:10px 18px;border:0;border-radius:999px;" +
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

  look();
  setInterval(look, 5000);
})();
