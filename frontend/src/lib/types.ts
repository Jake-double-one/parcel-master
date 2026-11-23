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
  /** Stops left on the delivery round, where the carrier tells; `about` marks a lower bound. */
  stops: { left: number; about: boolean } | null;
  /** The live view of the round vanished while the parcel is still on the van. */
  live_lost: boolean;
  /** What is inside, where an importer knows it. */
  /** Where the van is and where it is headed, while the carrier's live view tells. */
  map: { van: [number, number]; home: [number, number] | null; at: string | null } | null;
  contents: string[];
  /** Set when several orders share this parcel: which items came with which order. */
  contents_by_order: { reference: string; items: string[] }[] | null;
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
  /** In the bin since then; gone for good at `purge_at`. */
  deleted_at: string | null;
  purge_at: string | null;
  /** Found by a companion script; not tracked until the user takes it. */
  suggested: boolean;
  created_at: string;
  status: Status;
  parcel_count: number;
  finished_count: number;
  eta_from: string | null;
  eta_to: string | null;
  pickup_deadline: string | null;
  last_checked_at: string | null;
  parcels: Parcel[];
  /** Which parcel of its order this card is, as "2/3"; null for the whole order. */
  part: string | null;
  /** Other orders that came in the same box and share this card. */
  together: { id: number; reference: string; title: string }[];
}

export interface FieldSpec {
  key: string;
  type: "text" | "secret" | "choice";
  required: boolean;
  placeholder: string;
  /** For a "choice": the values to pick from; the first one is the default. */
  options: string[];
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
  /** Address of a userscript for the user's browser that passes on live news, if the module has one. */
  companion: string | null;
  configured: boolean;
  /** Off: nothing is asked, suggested or handed over to this carrier. */
  enabled: boolean;
  /** Open parcels this carrier tracks, and when one of them was last asked about. */
  open_parcels: number;
  last_checked_at: string | null;
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
  /** The commit the running image was built from, shortened. */
  build: string;
  /** Version of the companion script the server hands out; null if it has none. */
  companion_version: string | null;
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
  /** What happens to parcels a companion script comes across. */
  discovery: { mode: "button" | "suggest" | "auto" };
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
