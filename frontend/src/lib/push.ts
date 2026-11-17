import { api } from "./api";

export type PushState = "unsupported" | "blocked" | "on" | "off";

function supported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const base64 = base64url.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64 + "=".repeat((4 - (base64.length % 4)) % 4));
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

export async function pushState(): Promise<PushState> {
  if (!supported()) return "unsupported";
  if (Notification.permission === "denied") return "blocked";
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = await registration?.pushManager.getSubscription();
  return subscription ? "on" : "off";
}

export async function enablePush(): Promise<PushState> {
  if (!supported()) return "unsupported";
  if ((await Notification.requestPermission()) !== "granted") return "blocked";
  const registration = await navigator.serviceWorker.ready;
  const { public_key } = await api.pushKey();
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: keyBytes(public_key),
  });
  await api.pushSubscribe(subscription.toJSON());
  return "on";
}

export async function disablePush(): Promise<PushState> {
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = await registration?.pushManager.getSubscription();
  if (subscription) {
    await api.pushUnsubscribe(subscription.endpoint);
    await subscription.unsubscribe();
  }
  return "off";
}

/** The address the push service gave this browser, if it is signed up. */
export async function pushEndpoint(): Promise<string | null> {
  if (!supported()) return null;
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = await registration?.pushManager.getSubscription();
  return subscription?.endpoint ?? null;
}
