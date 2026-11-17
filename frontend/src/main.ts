import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/ibm-plex-mono/600.css";
import "@fontsource/instrument-sans/400.css";
import "@fontsource/instrument-sans/500.css";
import "@fontsource/instrument-sans/600.css";
import "./styles/skins.css";
import "./styles/base.css";

import { mount } from "svelte";
import App from "./App.svelte";
import { watchInstall } from "./lib/install.svelte";
import { installAutoSkin, syncThemeColor } from "./lib/skins";

installAutoSkin();
syncThemeColor();
watchInstall();

if ("serviceWorker" in navigator && import.meta.env.PROD) {
  navigator.serviceWorker.register("/sw.js").catch(() => {
    // Without a service worker the app still works; only push and install are missing.
  });
}

mount(App, { target: document.getElementById("app")! });
