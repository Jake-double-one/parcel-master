import { i18n, t } from "./i18n.svelte";
import { dayDistance, hasTime, relative } from "./time";

function fmt(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat(i18n.language, options);
}

export function clock(date: Date): string {
  return fmt({ hour: "2-digit", minute: "2-digit" }).format(date);
}

/** "today", "tomorrow" or a short weekday and date. */
export function dayLabel(date: Date, now: Date): string {
  const days = dayDistance(date, now);
  if (days === 0) return t("time.today");
  if (days === 1) return t("time.tomorrow");
  if (days === -1) return t("time.yesterday");
  return fmt({ weekday: "short", day: "numeric", month: "short" }).format(date);
}

/** The expected arrival, with a time window where the carrier gives one. */
export function etaLabel(from: string, to: string | null, now: Date): string {
  const start = new Date(from);
  let label = dayLabel(start, now);
  if (hasTime(start)) {
    const end = to ? new Date(to) : null;
    label +=
      " " +
      (end && end.getTime() !== start.getTime()
        ? t("shipment.eta_window", { from: clock(start), to: clock(end) })
        : clock(start));
  }
  return label;
}

export function ago(iso: string, now: Date): string {
  const value = relative(iso, now);
  return value.key === "now" ? t("time.now") : t(`time.${value.key}`, { n: value.n });
}

/** How long until a moment in the future, as a short phrase. */
export function until(iso: string, now: Date): string {
  const minutes = Math.round((new Date(iso).getTime() - now.getTime()) / 60_000);
  if (minutes <= 1) return t("time.soon");
  if (minutes < 90) return t("time.in_minutes", { n: minutes });
  return t("time.in_hours", { n: Math.round(minutes / 60) });
}

export function eventTime(iso: string | null, now: Date): string {
  if (!iso) return "";
  const date = new Date(iso);
  // Midnight sharp means the source named a day and no time.
  return hasTime(date) ? `${dayLabel(date, now)} ${clock(date)}` : dayLabel(date, now);
}

/** Days left to pick a parcel up, as a short sentence. */
export function pickupLabel(deadline: string, now: Date): string {
  const days = dayDistance(new Date(deadline), now);
  if (days < 0) return t("shipment.pickup_over");
  if (days === 0) return t("shipment.pickup_today");
  if (days === 1) return t("shipment.pickup_day");
  return t("shipment.pickup_days", { days });
}
