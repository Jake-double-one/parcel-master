<script lang="ts">
  import { t } from "../lib/i18n.svelte";
  import type { Shipment } from "../lib/types";
  import ParcelView from "./ParcelView.svelte";
  import StatusTag from "./StatusTag.svelte";

  let {
    shipment,
    now,
    busy = false,
    refreshing = false,
    onedit,
    onrefresh,
    onarchive,
    ondelete,
  }: {
    shipment: Shipment;
    now: Date;
    busy?: boolean;
    refreshing?: boolean;
    onedit: () => void;
    onrefresh: () => void;
    onarchive: (archived: boolean) => void;
    ondelete: () => void;
  } = $props();

  let confirmDelete = $state(false);

  const grouped = $derived(shipment.parcels.length > 1);
  const archived = $derived(shipment.archived_at !== null);
  // A shipment without parcels is not finished: something is still missing.
  const finished = $derived(
    shipment.parcel_count > 0 && shipment.finished_count === shipment.parcel_count,
  );
  // An imported order names where it came from, so its parcels read "Shop via DHL".
  const origin = $derived(
    shipment.source !== "manual" && shipment.source !== "import" && shipment.source !== "demo"
      ? shipment.source
      : "",
  );
  const title = $derived(
    shipment.title || shipment.parcels[0]?.tracking_number || t("shipment.untitled"),
  );

  function removeClicked() {
    if (confirmDelete) ondelete();
    else {
      confirmDelete = true;
      setTimeout(() => (confirmDelete = false), 4000);
    }
  }
</script>

<article
  class="card"
  id="shipment-{shipment.id}"
  class:arrived={finished && shipment.status === "delivered"}
>
  <header>
    <div class="titles">
      <p class="micro">
        {t(`direction.${shipment.direction}`)}{#if shipment.reference}
          · {t("shipment.reference", { reference: shipment.reference })}{/if}
      </p>
      <h3>{title}</h3>
    </div>
    {#if grouped}
      <div class="summary">
        <StatusTag status={shipment.status} />
        <span class="micro">
          {t("shipment.parcels", { done: shipment.finished_count, count: shipment.parcel_count })}
        </span>
      </div>
    {/if}
  </header>

  {#if shipment.note}
    <p class="note text">{shipment.note}</p>
  {/if}

  {#if grouped}
    <ul class="parcels">
      {#each shipment.parcels as parcel (parcel.id)}
        <li><ParcelView {parcel} {now} {origin} /></li>
      {/each}
    </ul>
  {:else if shipment.parcels[0]}
    <ParcelView parcel={shipment.parcels[0]} {now} {origin} />
  {/if}

  <footer class="row">
    <button type="button" class="chip" onclick={onedit}>{t("shipment.edit")}</button>
    {#if !finished && !archived}
      <button type="button" class="chip" onclick={onrefresh} disabled={busy}>
        {refreshing ? t("overview.refreshing") : t("shipment.refresh")}
      </button>
    {/if}
    <button type="button" class="chip" onclick={() => onarchive(!archived)}>
      {archived ? t("shipment.unarchive") : t("shipment.archive")}
    </button>
    <span class="spacer"></span>
    <button type="button" class="chip danger" onclick={removeClicked}>
      {confirmDelete ? t("shipment.delete_confirm") : t("shipment.delete")}
    </button>
  </footer>
</article>

<style>
  header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
  }

  .titles {
    min-width: 0;
  }

  h3 {
    font-size: 1.12rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    overflow-wrap: anywhere;
  }

  .summary {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 5px;
    text-align: right;
  }

  .text {
    margin: -4px 0 12px;
  }

  /* Parcels of one shipment sit indented under it, joined by a line. */
  .parcels {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .parcels li {
    margin-left: 6px;
    padding: 14px 0 14px 18px;
    border-left: 2px solid var(--line);
    position: relative;
  }

  .parcels li::before {
    content: "";
    position: absolute;
    left: -2px;
    top: 24px;
    width: 12px;
    border-top: 2px solid var(--line);
  }

  .parcels li + li {
    border-top: 0;
  }

  footer {
    margin-top: 16px;
    padding-top: 14px;
    border-top: 1px solid var(--line);
  }
</style>
