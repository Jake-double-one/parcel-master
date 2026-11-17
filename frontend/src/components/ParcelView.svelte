<script lang="ts">
  import { ago, dayLabel, etaLabel, eventTime, pickupLabel } from "../lib/format";
  import { t, tOr } from "../lib/i18n.svelte";
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
      <span class="eyebrow carrier">
        {origin && origin !== parcel.carrier_name
          ? t("shipment.via", { source: origin, carrier: parcel.carrier_name })
          : parcel.carrier_name}
      </span>
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
    {#if showStatus}<StatusTag status={parcel.status} />{/if}
  </div>

  <Rail status={parcel.status} />

  <p class="facts mono">
    {#if parcel.pickup_deadline}
      <span class:alert={new Date(parcel.pickup_deadline) < now}>
        {t("shipment.pickup_until", { date: dayLabel(new Date(parcel.pickup_deadline), now) })}
        · {pickupLabel(parcel.pickup_deadline, now)}
      </span>
    {:else if parcel.eta_from && !finished}
      <span>{t("shipment.eta", { date: etaLabel(parcel.eta_from, parcel.eta_to, now) })}</span>
    {/if}
    <span class="muted">
      {parcel.last_checked_at
        ? t("shipment.checked", { time: ago(parcel.last_checked_at, now) })
        : t("shipment.never_checked")}
    </span>
  </p>

  {#if parcel.description}
    <p class="description">
      {parcel.description}{#if parcel.location}<span class="muted"> · {parcel.location}</span>{/if}
    </p>
  {/if}

  {#if errorText}
    <p class="hint alert">{errorText}</p>
  {/if}

  <div class="row below">
    {#if parcel.tracking_url}
      <a class="chip" href={parcel.tracking_url} target="_blank" rel="noopener noreferrer">
        {t("shipment.open_carrier", { carrier: parcel.site_name })} ↗
      </a>
    {/if}
    {#if parcel.contents.length}
      <details class="fold">
        <summary>
          <svg class="chev" width="8" height="10" viewBox="0 0 8 10" aria-hidden="true">
            <path d="M1 1l5 4-5 4" fill="none" stroke="currentColor" stroke-width="1.6" />
          </svg>
          {t("shipment.contents")}
          <span class="count">{parcel.contents.length}</span>
        </summary>
        <ul class="contents">
          {#each parcel.contents as item}
            <li>{item}</li>
          {/each}
        </ul>
      </details>
    {/if}
    <details class="fold history">
      <summary>
        <svg class="chev" width="8" height="10" viewBox="0 0 8 10" aria-hidden="true">
          <path d="M1 1l5 4-5 4" fill="none" stroke="currentColor" stroke-width="1.6" />
        </svg>
        {t("shipment.history")}
      </summary>
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
    </details>
  </div>
</div>

<style>
  .top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .carrier {
    color: var(--ink);
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

  .description {
    margin-top: 8px;
    font-size: 0.92rem;
  }

  .count {
    color: var(--ink);
  }

  .contents {
    list-style: none;
    margin: 6px 0 4px;
    padding: 0;
    font-size: 0.88rem;
    color: var(--muted);
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
    align-items: flex-start;
  }

  /* Closed, the history is just a link in the row; open, it takes the full width. */
  .fold[open] {
    flex: 1 1 100%;
  }

  .fold > summary {
    min-height: 36px;
    width: fit-content;
    padding: 0 4px;
  }

  ol {
    list-style: none;
    margin: 6px 0 0;
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
    margin-top: 6px;
  }

  @media (max-width: 560px) {
    li {
      grid-template-columns: 1fr;
      gap: 2px;
    }
  }
</style>
