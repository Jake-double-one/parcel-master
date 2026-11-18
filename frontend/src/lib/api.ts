import type {
  Carrier,
  Meta,
  Settings,
  Shipment,
  ShipmentDraft,
  SyncState,
  SystemStatus,
} from "./types";

/** A failed request. `code` is a key below "error." in the language files. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public details: Record<string, unknown> = {},
  ) {
    super(code);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "offline");
  }
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.error) {
      throw new ApiError(response.status, String(detail.error), detail);
    }
    throw new ApiError(response.status, "generic");
  }
  return data as T;
}

export interface LogReport {
  level: string;
  entries: { time: string; level: string; source: string; message: string }[];
}

export interface TestReport {
  apprise: { ok: boolean } | null;
  push: { ok: boolean; sent: number; subscriptions: number; error: string } | null;
}

export const api = {
  meta: () => request<Meta>("GET", "/api/meta"),
  texts: (language: string) => request<Record<string, string>>("GET", `/api/i18n/${language}`),
  login: (password: string) => request("POST", "/api/auth/login", { password }),
  startDemo: (language: string) => request("POST", "/api/auth/demo", { language }),
  logout: () => request("POST", "/api/auth/logout"),
  carriers: () => request<Carrier[]>("GET", "/api/carriers"),
  detect: (text: string) =>
    request<{ matches: { carrier: string; tracking_number: string }[] }>("POST", "/api/detect", {
      text,
    }),
  shipments: (archived: boolean) => request<Shipment[]>("GET", `/api/shipments?archived=${archived}`),
  create: (draft: ShipmentDraft) => request<Shipment>("POST", "/api/shipments", draft),
  update: (id: number, draft: ShipmentDraft) => request<Shipment>("PUT", `/api/shipments/${id}`, draft),
  remove: (id: number) => request<void>("DELETE", `/api/shipments/${id}`),
  archive: (id: number, archived: boolean) =>
    request<Shipment>("POST", `/api/shipments/${id}/archive`, { archived }),
  refresh: (id: number) => request<Shipment>("POST", `/api/shipments/${id}/refresh`),
  settings: () => request<Settings>("GET", "/api/settings"),
  saveSettings: (settings: Partial<Settings>) => request<Settings>("PUT", "/api/settings", settings),
  testNotification: (endpoint?: string) =>
    request<TestReport>("POST", "/api/notifications/test", endpoint ? { endpoint } : undefined),
  pushKey: () => request<{ public_key: string }>("GET", "/api/push/key"),
  pushSubscribe: (subscription: PushSubscriptionJSON) =>
    request<void>("POST", "/api/push/subscriptions", subscription),
  pushUnsubscribe: (endpoint: string) => request<void>("POST", "/api/push/unsubscribe", { endpoint }),
  status: () => request<SystemStatus>("GET", "/api/status"),
  log: () => request<LogReport>("GET", "/api/log"),
  takeNotices: () => request<{ auto_archived: number }>("POST", "/api/notices/take"),
  syncImporter: (id: string) => request<SyncState>("POST", `/api/importers/${id}/sync`),
  connectImporter: (id: string, credentials: Record<string, string>) =>
    request<SyncState>("POST", `/api/importers/${id}/connect`, credentials),
  importOrder: (id: string, reference: string) =>
    request<Shipment>("POST", `/api/importers/${id}/import`, { reference }),
  disconnectImporter: (id: string) => request<SyncState>("POST", `/api/importers/${id}/disconnect`),
  exportData: () => request<unknown>("GET", "/api/export"),
  importData: (data: unknown) =>
    request<{ imported: number; skipped: number }>("POST", "/api/import", data),
};
