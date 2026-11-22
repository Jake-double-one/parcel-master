<script lang="ts">
  import { ago, dayLabel, etaLabel, eventTime, pickupLabel } from "../lib/format";
  import { t, tOr } from "../lib/i18n.svelte";
  import { dayDistance } from "../lib/time";
  import type { Parcel } from "../lib/types";
  import Rail from "./Rail.svelte";
  import StatusTag from "./StatusTag.svelte";

  let {
    parcel,
    now,
    showStatus = true,
    origin = "",
  }: { parcel: Parcel; now: Date; showStatus?: boolean; origin?: string } = $props();

  let copied = $state(false);
  // One panel at a time, so opening one never pushes the other button around.
  let panel = $state<"contents" | "history" | null>(null);

  function toggle(name: "contents" | "history") {
    panel = panel === name ? null : name;
  }

  async function copyNumber() {
    try {
      await navigator.clipboard.writeText(parcel.tracking_number);
      copied = true;
      setTimeout(() => (copied = false), 1800);
    } catch {
      // Without clipboard access the number stays selectable on the page.
    }
  }

  const finished = $derived(parcel.status === "delivered" || parcel.status === "returned");
  const errorText = $derived(
    parcel.last_error
      ? tOr(`carrier_error.${parcel.last_error.code}`, t("carrier_error.unavailable"))
      : "",
  );
</script>

<div class="parcel">
  <div class="top">
    <div class="who">
      {#if origin && origin !== parcel.carrier_name}
        <span class="eyebrow">{t("shipment.through", { source: origin })}</span>
      {/if}
      <button
        type="button"
        class="number mono"
        onclick={copyNumber}
        title={t("shipment.copy")}
        aria-label="{t('shipment.copy')}: {parcel.tracking_number}"
      >
        {copied ? t("shipment.copied") : parcel.tracking_number}
      </button>
    </div>
    {#if showStatus}
      <div class="tags"><StatusTag status={parcel.status} /></div>
    {/if}
  </div>

  <Rail status={parcel.status} />

  <p class="facts mono">
    {#if parcel.pickup_deadline}
      <span
        class:alert={new Date(parcel.pickup_deadline) < now}
        class:warn={new Date(parcel.pickup_deadline) >= now &&
          dayDistance(new Date(parcel.pickup_deadline), now) <= 2}
      >
        {t("shipment.pickup_until", { date: dayLabel(new Date(parcel.pickup_deadline), now) })}
        · {pickupLabel(parcel.pickup_deadline, now)}
      </span>
    {:else if parcel.eta_from && !finished}
      <span>{t("shipment.eta", { date: etaLabel(parcel.eta_from, parcel.eta_to, now) })}</span>
    {/if}
    {#if parcel.live_lost && !parcel.stops && !finished}
      <span class="warn">{t("shipment.live_lost")}</span>
    {/if}
    {#if parcel.stops && !finished}
      <span>
        {parcel.stops.left <= 0
          ? t("shipment.stops_next")
          : parcel.stops.left === 1
            ? t("shipment.stops_one")
          : t(parcel.stops.about ? "shipment.stops_about" : "shipment.stops", {
              count: parcel.stops.left,
            })}
      </span>
    {/if}
    <span class="muted">
      {parcel.last_checked_at
        ? t("shipment.checked", { time: ago(parcel.last_checked_at, now) })
        : t("shipment.never_checked")}
    </span>
  </p>

  {#if parcel.description}
    <p class="description">
      {#if parcel.last_event_at}
        <span class="mono since">{eventTime(parcel.last_event_at, now)}</span>
      {/if}
      {parcel.description}{#if parcel.location}<span class="muted"> · {parcel.location}</span>{/if}
    </p>
  {/if}

  {#if errorText}
    <p class="hint alert">{errorText}</p>
  {/if}

  <!-- The buttons keep their place; what they open appears below the row. -->
  <div class="row below">
    <!-- Who delivers it; the name opens the parcel on that service's website. -->
    {#if parcel.tracking_url}
      <a
        class="chip carrier"
        href={parcel.tracking_url}
        target="_blank"
        rel="noopener noreferrer"
        title={t("shipment.open_carrier", { carrier: parcel.site_name })}
        aria-label={t("shipment.open_carrier", { carrier: parcel.site_name })}
      >
        <span class="name">{parcel.site_name}</span>
        <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor"
          stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M7 1.5h3.5V5" />
          <path d="M10.5 1.5 5.5 6.5" />
          <path d="M9 7.5v2.25c0 .4-.35.75-.75.75h-6c-.4 0-.75-.35-.75-.75v-6c0-.4.35-.75.75-.75H4.5" />
        </svg>
      </a>
    {:else}
      <span class="chip carrier still"><span class="name">{parcel.carrier_name}</span></span>
    {/if}
    {#if parcel.contents.length}
      <button
        type="button"
        class="chip"
        aria-pressed={panel === "contents"}
        aria-expanded={panel === "contents"}
        onclick={() => toggle("contents")}
      >
        {t("shipment.contents")}
        <span class="count">{parcel.contents.length}</span>
      </button>
    {/if}
    <button
      type="button"
      class="chip"
      aria-pressed={panel === "history"}
      aria-expanded={panel === "history"}
      onclick={() => toggle("history")}
    >
      {t("shipment.history")}
      {#if parcel.events.length}<span class="count">{parcel.events.length}</span>{/if}
    </button>
  </div>

  {#if panel === "contents"}
    {#if parcel.contents_by_order}
      <!-- Several orders in this box: their items, order by order. -->
      {#each parcel.contents_by_order as order}
        <p class="micro from">{t("shipment.from_order", { reference: order.reference })}</p>
        <ul class="contents grouped">
          {#each order.items as item}
            <li>{item}</li>
          {/each}
        </ul>
      {/each}
    {:else}
      <ul class="contents">
        {#each parcel.contents as item}
          <li>{item}</li>
        {/each}
      </ul>
    {/if}
  {:else if panel === "history"}
    {#if parcel.events.length}
      <ol>
        {#each parcel.events as event}
          <li>
            <span class="mono when">{eventTime(event.timestamp, now)}</span>
            <span>
              {event.description}{#if event.location}<span class="muted"> · {event.location}</span>{/if}
            </span>
          </li>
        {/each}
      </ol>
    {:else}
      <p class="note none">{t("shipment.no_events")}</p>
    {/if}
  {/if}
</div>

<style>
  .since {
    display: block;
    font-size: 0.72rem;
    color: var(--muted);
    margin-bottom: 2px;
  }

  .top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .tags {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 6px;
  }

  .carrier .name {
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink);
  }

  .carrier svg {
    opacity: 0.75;
  }

  /* Without a link the name is only a label: it does not react like a button. */
  .carrier.still,
  .carrier.still:hover {
    cursor: default;
    border-color: var(--line);
  }

  .who {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 2px 12px;
    min-width: 0;
  }

  .number {
    padding: 0;
    border: 0;
    background: none;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: copy;
    overflow-wrap: anywhere;
    text-align: left;
  }

  .number:hover {
    color: var(--dusk);
  }

  .facts {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 16px;
    font-size: 0.78rem;
  }

  .facts .alert {
    color: var(--alert);
  }

  .facts .warn {
    color: var(--warn);
  }

  .description {
    margin-top: 8px;
    font-size: 0.92rem;
  }

  .count {
    color: var(--ink);
  }

  .contents {
    list-style: none;
    margin: 12px 0 0;
    padding: 0;
    font-size: 0.88rem;
    color: var(--muted);
  }

  .from {
    margin: 14px 0 0;
    color: var(--muted);
  }

  .contents.grouped {
    margin-top: 4px;
  }

  .contents li {
    display: block;
    padding: 2px 0 2px 14px;
    border: 0;
    position: relative;
  }

  .contents li::before {
    content: "";
    position: absolute;
    left: 0;
    top: 0.75em;
    width: 6px;
    border-top: 1px solid var(--muted);
  }

  .hint {
    margin: 10px 0 0;
  }

  .below {
    margin-top: 12px;
  }

  ol {
    list-style: none;
    margin: 12px 0 0;
    padding: 0;
  }

  li {
    display: grid;
    grid-template-columns: 15ch 1fr;
    gap: 12px;
    padding: 9px 0;
    border-top: 1px solid var(--line);
    font-size: 0.88rem;
  }

  .when {
    font-size: 0.74rem;
    color: var(--muted);
    padding-top: 2px;
  }

  .none {
    margin-top: 12px;
  }

  /* On the narrowest phones the three buttons still share one line. */
  @media (max-width: 400px) {
    .below {
      gap: 6px;
    }

    .below .chip {
      padding-inline: 9px;
    }
  }

  @media (max-width: 560px) {
    li {
      grid-template-columns: 1fr;
      gap: 2px;
    }
  }
</style>
