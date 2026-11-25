// ==UserScript==
// @name         Parcel Master: GLS
// @namespace    parcel-master
// @version      4
// @description  Passes the history GLS' tracking page shows you on to your Parcel Master and adds a button that takes the parcel.
// @match        https://www.gls-pakete.de/*
// @grant        GM_xmlhttpRequest
// @connect      __HOST__
// @run-at       document-idle
// ==/UserScript==

// GLS shows a parcel's history only on this page. This script reads it from
// what the page displays and hands it to your own server: the parcel number,
// the steps, the sender's name and, on the delivery day, the window and the
// stops left. It asks GLS nothing by itself, leaves the recipient out and
// sends nothing anywhere else. While the live view is open it presses the
// page's own "Update" button for you.
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
    // The live view of the delivery day: the window as it moves, and the stops.
    // GLS swaps the two lines as the van comes near: first the window on top and
    // the stops below, then the stops on top. Both are read from either line.
    const lines = [...document.querySelectorAll('[data-testid="heading"], [data-testid="subheading"]')];
    const times = lines.flatMap((line) => [...line.querySelectorAll("time[datetime]")]);
    const live = !!document.querySelector('[data-testid="rtt-live-update"]');
    const span = live && times.length === 2
      ? { from: times[0].getAttribute("datetime"), to: times[1].getAttribute("datetime") }
      : null;
    // "50+" is no number worth passing on; only a real count is.
    const count = lines.map((line) => /^(\d+)\s*Stopp/.exec(text(line))).find(Boolean);
    const stops = live && count ? Number(count[1]) : null;
    if (!/^\d{11,12}$/.test(number) || (!list && !span && stops === null)) return null;
    const events = [];
    for (const time of list ? list.querySelectorAll("time[datetime]") : []) {
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
    const named = document.querySelector('[data-testid="sender-name"] abbr');
    return {
      number: number,
      sender: text(sender) || (named && named.getAttribute("title")) || "",
      events: events,
      window: span,
      stops: stops,
    };
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

  // -- pressing "Update" for the user ---------------------------------------
  // The live view only moves when its button is pressed. While it is open the
  // script presses it at a calm pace, faster as the van comes near, and shows
  // the seconds left next to it. A click on that note pauses and resumes.
  let left = 0; // seconds until the next press
  let paused = false;

  function updateButton() {
    const button = document.querySelector('[data-testid="rtt-live-update"] button:not([data-parcel-master-refresh])');
    return button && !button.disabled ? button : null;
  }

  function pace() {
    const stops = parcel ? parcel.stops : null;
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
        "display:block;margin:6px auto 0;padding:3px 8px;border-radius:999px;border:1px solid #888;" +
        "background:transparent;color:inherit;font:inherit;font-size:11px;white-space:nowrap;" +
        "font-variant-numeric:tabular-nums;cursor:pointer;";
      label.addEventListener("click", () => {
        paused = !paused;
        tick();
      });
      button.parentElement.appendChild(label);
    }
    // Short and on one line: the column it sits in is narrow.
    const words = "Parcel Master · " + (paused ? (german ? "pausiert" : "paused") : left + " s");
    const hint = german
      ? "Parcel Master drückt „Update“ für dich. Klick: pausieren oder fortsetzen."
      : "Parcel Master presses “Update” for you. Click to pause or resume.";
    if (label.title !== hint) label.title = hint;
    if (label.textContent !== words) label.textContent = words;
  }

  function tick() {
    const button = updateButton();
    if (!button) {
      left = 0; // no live view open: start afresh when one appears
      return;
    }
    left = left <= 0 ? pace() : Math.min(left, pace()); // sooner once the van is near
    if (!paused) {
      left -= 1;
      if (left <= 0) {
        button.click();
        left = pace();
      }
    }
    note(button);
  }

  // A worker keeps time in a tab that is not in front, where the page's own
  // timers are slowed down; if the page does not allow one, a plain timer does.
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
