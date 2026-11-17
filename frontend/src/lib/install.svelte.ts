/**
 * Installing the app on a phone or desktop. Chromium browsers hand out a
 * prompt we can show on a button; Safari on iPhone only installs through its
 * share menu, and other browsers through their own menu.
 */
interface InstallPrompt extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

export type InstallState = "installed" | "ready" | "ios" | "manual";

let prompt = $state<InstallPrompt | null>(null);
let installed = $state(false);

function standalone(): boolean {
  return (
    matchMedia("(display-mode: standalone)").matches ||
    (navigator as { standalone?: boolean }).standalone === true
  );
}

function ios(): boolean {
  // An iPad reports itself as a Mac, but only it has a touch screen.
  return (
    /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
  );
}

/** Must run early: the browser offers its prompt once, soon after loading. */
export function watchInstall(): void {
  installed = standalone();
  addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    prompt = event as InstallPrompt;
  });
  addEventListener("appinstalled", () => {
    prompt = null;
    installed = true;
  });
}

export const install = {
  get state(): InstallState {
    if (installed) return "installed";
    if (prompt) return "ready";
    return ios() ? "ios" : "manual";
  },
  async run(): Promise<boolean> {
    if (!prompt) return false;
    const asked = prompt;
    prompt = null;
    await asked.prompt();
    return (await asked.userChoice).outcome === "accepted";
  },
};
