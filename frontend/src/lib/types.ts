export type Status =
  | "unknown"
  | "registered"
  | "in_transit"
  | "out_for_delivery"
  | "ready_for_pickup"
  | "delivered"
  | "problem"
  | "returned";

export type Direction = "incoming" | "outgoing" | "return";

export interface TrackingEvent {
  timestamp: string | null;
  status: Status;
  description: string;
  location: string | null;
}

export interface Parcel {
  id: number;
  carrier: string;
  carrier_name: string;
  tracking_number: string;
  extra: Record<string, string>;
  status: Status;
  description: string;
  location: string | null;
  eta_from: string | null;
  eta_to: string | null;
  last_event_at: string | null;
  delivered_at: string | null;
  pickup_deadline: string | null;
  last_checked_at: string | null;
  last_error: { code: string; detail: string } | null;
  tracking_url: string | null;
  /** What is inside, where an importer knows it. */
  contents: string[];
  /** Whose website `tracking_url` opens. */
  site_name: string;
  events: TrackingEvent[];
}

export interface Shipment {
  id: number;
  title: string;
  direction: Direction;
  note: string;
  source: string;
  reference: string;
  archived_at: string | null;
  created_at: string;
  status: Status;
  parcel_count: number;
  finished_count: number;
  eta_from: string | null;
  eta_to: string | null;
  pickup_deadline: string | null;
  last_checked_at: string | null;
  parcels: Parcel[];
}

export interface FieldSpec {
  key: string;
  type: "text" | "secret";
  required: boolean;
  placeholder: string;
}

export interface SyncState {
  /** Minutes between two background syncs, and the module's own default. */
  interval_minutes: number;
  default_interval_minutes: number;
  /** When the next background sync is due; null while none is scheduled. */
  next_sync_at: string | null;
  last_sync_at: string | null;
  last_attempt_at: string | null;
  last_error: { code: string; detail: string } | null;
  found: number | null;
  /** The account whose sign-in was checked; null while not connected. */
  account: string | null;
}

/** How a carrier module is doing, and which way it asks if it knows several. */
export interface Health {
  state: "off" | "untested" | "ok" | "failing";
  source: string | null;
}

export interface Carrier {
  health: Health;
  id: string;
  name: string;
  country: string;
  /** "tracker": added by number. "importer": finds its shipments itself. */
  kind: "tracker" | "importer";
  sync: SyncState | null;
  /** An importer whose orders can also be added one by one, by their number. */
  by_reference: boolean;
  configured: boolean;
  settings_fields: FieldSpec[];
  parcel_fields: FieldSpec[];
  /** Defaults from the settings for parcel fields, e.g. the user's postal code. */
  parcel_defaults: Record<string, string>;
  default_daily_budget: number | null;
  daily_budget: number | null;
  used_today: number;
}

export interface Meta {
  version: string;
  login_required: boolean;
  password_set: boolean;
  demo_available: boolean;
  demo: boolean;
  languages: { code: string; name: string }[];
  language: string | null;
}

export interface Settings {
  language: string;
  timezone: string;
  polling: {
    interval_minutes: number;
    stale_days: number;
    auto_archive_days: number;
    /** Ask carriers less: night rest, slower lookups, later hand-over. */
    saver: { enabled: boolean; from: string; to: string };
  };
  carriers: Record<string, Record<string, string | number | null>>;
  notifications: {
    apprise_urls: string[];
    push_enabled: boolean;
    events: Record<string, boolean>;
    quiet_hours: { enabled: boolean; from: string; to: string };
  };
}

export interface ModuleStatus {
  health: Health;
  id: string;
  name: string;
  configured: boolean;
  open_parcels: number;
  daily_budget: number | null;
  used_today: number;
  last_success_at: string | null;
  last_error: { code: string; detail: string } | null;
  failing_parcels: number;
  kind: "tracker" | "importer";
  sync: SyncState | null;
}

export interface SystemStatus {
  version: string;
  scheduler: { running: boolean; last_run: string | null };
  modules: ModuleStatus[];
  push_subscriptions: number;
}

export interface ParcelDraft {
  id?: number;
  carrier: string;
  tracking_number: string;
  extra: Record<string, string>;
}

export interface ShipmentDraft {
  title: string;
  direction: Direction;
  note: string;
  parcels: ParcelDraft[];
}
