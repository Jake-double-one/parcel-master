import { describe, expect, it } from "vitest";
import { dayDistance, groupOf, groupShipments, matches, progress, relative } from "./time";
import type { Shipment } from "./types";

const now = new Date(2026, 10, 17, 12, 0);

function shipment(overrides: Partial<Shipment>): Shipment {
  return {
    id: 1,
    title: "Boots",
    direction: "incoming",
    note: "",
    source: "manual",
    reference: "",
    archived_at: null,
    deleted_at: null,
    purge_at: null,
    suggested: false,
    created_at: "2026-11-15T10:00:00+00:00",
    status: "in_transit",
    parcel_count: 1,
    finished_count: 0,
    eta_from: null,
    eta_to: null,
    pickup_deadline: null,
    last_checked_at: null,
    parcels: [],
    ...overrides,
  };
}

const at = (day: number, hour = 14) => new Date(2026, 10, day, hour).toISOString();

describe("groupOf", () => {
  it("sorts by expected arrival", () => {
    expect(groupOf(shipment({ eta_from: at(17) }), now)).toBe("today");
    expect(groupOf(shipment({ eta_from: at(18, 0) }), now)).toBe("tomorrow");
    expect(groupOf(shipment({ eta_from: at(21) }), now)).toBe("week");
    expect(groupOf(shipment({ eta_from: at(28) }), now)).toBe("later");
    expect(groupOf(shipment({}), now)).toBe("undated");
  });

  it("keeps an overdue estimate at the top", () => {
    expect(groupOf(shipment({ eta_from: at(15) }), now)).toBe("today");
  });

  it("lets the status win over the date", () => {
    expect(groupOf(shipment({ status: "problem", eta_from: at(28) }), now)).toBe("attention");
    expect(groupOf(shipment({ status: "ready_for_pickup" }), now)).toBe("attention");
    expect(groupOf(shipment({ status: "out_for_delivery" }), now)).toBe("today");
    expect(groupOf(shipment({ status: "delivered", eta_from: at(17) }), now)).toBe("done");
  });
});

describe("groupShipments", () => {
  it("orders groups and sorts each by arrival", () => {
    const grouped = groupShipments(
      [
        shipment({ id: 1, eta_from: at(28) }),
        shipment({ id: 2, eta_from: at(17, 16) }),
        shipment({ id: 3, status: "problem" }),
        shipment({ id: 4, eta_from: at(17, 9) }),
      ],
      now,
    );
    expect(grouped.map((g) => g.group)).toEqual(["attention", "today", "later"]);
    expect(grouped[1].items.map((s) => s.id)).toEqual([4, 2]);
  });
});

describe("helpers", () => {
  it("counts calendar days, not 24-hour blocks", () => {
    expect(dayDistance(new Date(2026, 10, 18, 0, 5), now)).toBe(1);
    expect(dayDistance(new Date(2026, 10, 17, 23, 59), now)).toBe(0);
    expect(dayDistance(new Date(2026, 10, 16, 23, 59), now)).toBe(-1);
  });

  it("searches title, number and carrier", () => {
    const item = shipment({
      title: "Hiking boots",
      parcels: [{ tracking_number: "00340434", carrier_name: "DHL" } as never],
    });
    expect(matches(item, "boots")).toBe(true);
    expect(matches(item, "dhl 0434")).toBe(true);
    expect(matches(item, "hermes")).toBe(false);
    expect(matches(item, "  ")).toBe(true);
  });

  it("describes how long ago something happened", () => {
    expect(relative(new Date(2026, 10, 17, 11, 59, 30).toISOString(), now)).toEqual({ key: "now" });
    expect(relative(new Date(2026, 10, 17, 11, 48).toISOString(), now)).toEqual({ key: "minutes", n: 12 });
    expect(relative(new Date(2026, 10, 17, 9, 0).toISOString(), now)).toEqual({ key: "hours", n: 3 });
    expect(relative(new Date(2026, 10, 14, 12, 0).toISOString(), now)).toEqual({ key: "days", n: 3 });
  });

  it("fills the rail only on arrival", () => {
    expect(progress("delivered")).toBe(1);
    expect(progress("in_transit")).toBeLessThan(progress("out_for_delivery"));
  });
});

describe("found shipments", () => {
  it("stand apart until the user takes them", () => {
    expect(groupOf(shipment({ suggested: true, status: "out_for_delivery" }), now)).toBe("found");
  });
});
