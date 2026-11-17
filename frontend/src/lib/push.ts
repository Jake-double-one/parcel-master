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

function sameKey(subscription: PushSubscription, key: Uint8Array): boolean {
  const used = subscription.options.applicationServerKey;
  if (!used) return false;
  const bytes = new Uint8Array(used);
  return bytes.length === key.length && bytes.every((byte, index) => byte === key[index]);
}

async function worker(): Promise<ServiceWorkerRegistration> {
  const late = new Promise<never>((_, reject) =>
    setTimeout(() => reject(new Error("The service worker did not start.")), 10_000),
  );
  return Promise.race([navigator.serviceWorker.ready, late]);
}

export async function enablePush(): Promise<PushState> {
  if (!supported()) return "unsupported";
  if ((await Notification.requestPermission()) !== "granted") return "blocked";
  const registration = await worker();
  const { public_key } = await api.pushKey();
  const key = keyBytes(public_key);
  // A sign-up made for another server key cannot be reused or replaced in place.
  const old = await registration.pushManager.getSubscription();
  if (old && !sameKey(old, key)) await old.unsubscribe();
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: key,
  });
  await api.pushSubscribe(subscription.toJSON());
  return "on";
}

/**
 * Tells the server again about a sign-up the browser already holds, in case
 * the server lost it. A sign-up for another server key is dropped.
 */
export async function syncPush(): Promise<PushState> {
  const state = await pushState();
  if (state !== "on") return state;
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = await registration?.pushManager.getSubscription();
  if (!subscription) return "off";
  const { public_key } = await api.pushKey();
  if (!sameKey(subscription, keyBytes(public_key))) {
    await subscription.unsubscribe();
    return "off";
  }
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
