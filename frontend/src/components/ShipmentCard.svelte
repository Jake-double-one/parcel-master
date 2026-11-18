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
    onrestore,
    onaccept,
  }: {
    shipment: Shipment;
    now: Date;
    busy?: boolean;
    refreshing?: boolean;
    onedit: () => void;
    onrefresh: () => void;
    onarchive: (archived: boolean) => void;
    ondelete: () => void;
    onrestore: () => void;
    onaccept: () => void;
  } = $props();

  let confirmDelete = $state(false);

  const grouped = $derived(shipment.parcels.length > 1);
  const archived = $derived(shipment.archived_at !== null);
  const binned = $derived(shipment.deleted_at !== null);
  // A shipment without parcels is not finished: something is still missing.
  const finished = $derived(
    shipment.parcel_count > 0 && shipment.finished_count === shipment.parcel_count,
  );
  // An imported order names where it came from, so its parcels read "Shop via DHL".
  const origin = $derived(
    !["manual", "import", "demo", "discovered"].includes(shipment.source)
      ? shipment.source
      : "",
  );
  const title = $derived(
    shipment.title || shipment.parcels[0]?.tracking_number || t("shipment.untitled"),
  );

  function removeClicked() {
    // Only the last step asks twice: the bin itself forgives.
    if (confirmDelete || !binned) ondelete();
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
        {#if shipment.suggested}<span class="new">{t("shipment.new")}</span>{/if}
        {t(`direction.${shipment.direction}`)}{#if shipment.reference}
          · {t("shipment.reference", { reference: shipment.reference })}{/if}
      </p>
      <!-- One line; the pointer shows the rest. -->
      <h3 {title}>{title}</h3>
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

  {#if binned && shipment.purge_at}
    <p class="note">
      {t("shipment.purge_on", { date: new Date(shipment.purge_at).toLocaleDateString() })}
    </p>
  {/if}

  <footer class="row">
    {#if binned}
      <button type="button" class="chip" onclick={onrestore}>{t("shipment.restore")}</button>
      <span class="spacer"></span>
      <button type="button" class="chip danger" onclick={removeClicked}>
        {confirmDelete ? t("shipment.delete_confirm") : t("shipment.delete_for_good")}
      </button>
    {:else if shipment.suggested}
      <button type="button" class="chip primary" onclick={onaccept}>
        {t("shipment.accept")}
      </button>
      <span class="spacer"></span>
      <button type="button" class="chip" onclick={ondelete}>{t("shipment.discard")}</button>
    {:else}
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
        {t("shipment.delete")}
      </button>
    {/if}
  </footer>
</article>

<style>
  .new {
    margin-right: 0.4em;
    padding: 0.1em 0.5em;
    border-radius: 999px;
    border: 1px solid var(--dusk);
    background: var(--dusk-soft);
    color: var(--ink);
  }
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
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
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
