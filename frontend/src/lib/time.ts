import type { Shipment, Status } from "./types";

export type Group = "found" | "attention" | "today" | "tomorrow" | "week" | "later" | "undated" | "done";
export const GROUPS: Group[] = ["found", "attention", "today", "tomorrow", "week", "later", "undated", "done"];

const FINAL: Status[] = ["delivered", "returned"];

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** Whole calendar days from `now` to `date`; negative when it is in the past. */
export function dayDistance(date: Date, now: Date): number {
  return Math.round((startOfDay(date) - startOfDay(now)) / 86_400_000);
}

/** Where a shipment belongs in the overview. */
export function groupOf(shipment: Shipment, now: Date): Group {
  if (shipment.suggested) return "found"; // came across it; the user has not decided yet
  if (FINAL.includes(shipment.status)) return "done";
  if (shipment.status === "problem" || shipment.status === "ready_for_pickup") return "attention";
  if (shipment.status === "out_for_delivery") return "today";
  if (!shipment.eta_from) return "undated";
  const days = dayDistance(new Date(shipment.eta_from), now);
  if (days <= 0) return "today"; // overdue estimates still belong at the top
  if (days === 1) return "tomorrow";
  if (days <= 6) return "week";
  return "later";
}

export function groupShipments(shipments: Shipment[], now: Date): { group: Group; items: Shipment[] }[] {
  const buckets = new Map<Group, Shipment[]>();
  for (const shipment of shipments) {
    const group = groupOf(shipment, now);
    buckets.set(group, [...(buckets.get(group) ?? []), shipment]);
  }
  const byEta = (a: Shipment, b: Shipment) =>
    (a.eta_from ?? "9").localeCompare(b.eta_from ?? "9") || b.created_at.localeCompare(a.created_at);
  return GROUPS.filter((group) => buckets.has(group)).map((group) => ({
    group,
    items: buckets.get(group)!.sort(byEta),
  }));
}

export function matches(shipment: Shipment, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const haystack = [
    shipment.title,
    shipment.note,
    shipment.reference,
    ...(shipment.together ?? []).flatMap((o) => [o.reference, o.title]),
    ...shipment.parcels.flatMap((p) => [p.tracking_number, p.carrier_name]),
  ]
    .join(" ")
    .toLowerCase();
  return needle.split(/\s+/).every((word) => haystack.includes(word));
}

/** How far along a parcel is, 0 to 1, for the progress rail. */
export function progress(status: Status): number {
  return {
    unknown: 0,
    registered: 0.12,
    in_transit: 0.5,
    problem: 0.5,
    out_for_delivery: 0.82,
    ready_for_pickup: 0.9,
    delivered: 1,
    returned: 1,
  }[status];
}

export type Relative =
  | { key: "now" }
  | { key: "minutes" | "hours" | "days"; n: number };

export function relative(iso: string, now: Date): Relative {
  const seconds = Math.max(0, (now.getTime() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return { key: "now" };
  if (seconds < 3600) return { key: "minutes", n: Math.floor(seconds / 60) };
  if (seconds < 86_400) return { key: "hours", n: Math.floor(seconds / 3600) };
  return { key: "days", n: Math.floor(seconds / 86_400) };
}

/** True when a timestamp carries a time of day worth showing (not midnight). */
export function hasTime(date: Date): boolean {
  return date.getHours() !== 0 || date.getMinutes() !== 0;
}
