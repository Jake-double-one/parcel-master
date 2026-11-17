<script lang="ts">
  import { onMount } from "svelte";
  import Login from "./components/Login.svelte";
  import SettingsDialog from "./components/SettingsDialog.svelte";
  import ShipmentCard from "./components/ShipmentCard.svelte";
  import ShipmentDialog from "./components/ShipmentDialog.svelte";
  import { api, ApiError } from "./lib/api";
  import { browserLanguage, i18n, t, tOr } from "./lib/i18n.svelte";
  import { groupOf, groupShipments, matches } from "./lib/time";
  import type { Carrier, Meta, Shipment } from "./lib/types";

  const REFRESH_MS = 30_000;
  const PENDING_REFRESH_MS = 4_000;

  let meta = $state<Meta | null>(null);
  let shipments = $state<Shipment[]>([]);
  let carriers = $state<Carrier[]>([]);
  let loaded = $state(false);
  let showArchive = $state(false);
  let query = $state("");
  let now = $state(new Date());
  let editing = $state<{ shipment: Shipment | null; prefill: string } | null>(null);
  let settingsOpen = $state(false);
  let busy = $state<number[]>([]);
  let toast = $state<{ text: string; undo?: () => void } | null>(null);
  let toastTimer: ReturnType<typeof setTimeout> | undefined;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;

  const visible = $derived(shipments.filter((s) => matches(s, query)));
  const groups = $derived(groupShipments(visible, now));
  const open = $derived(shipments.filter((s) => groupOf(s, now) !== "done"));
  const figures = $derived({
    today: open.filter((s) => groupOf(s, now) === "today").length,
    pickup: open.filter((s) => s.status === "ready_for_pickup").length,
    problem: open.filter((s) => s.status === "problem").length,
    arrived: shipments.length - open.length,
  });
  // A parcel that was just added has no answer from its carrier yet.
  const pending = $derived(
    shipments.some((s) => s.parcels.some((p) => !p.last_checked_at && !p.last_error)),
  );

  function notify(text: string, undo?: () => void) {
    clearTimeout(toastTimer);
    toast = { text, undo };
    toastTimer = setTimeout(() => (toast = null), undo ? 7000 : 4000);
  }

  function report(caught: unknown) {
    const failure = caught instanceof ApiError ? caught : new ApiError(0, "generic");
    if (failure.status === 401) {
      boot(); // the session ended; show the login again
      return;
    }
    notify(tOr(`error.${failure.code}`, t("error.generic")));
  }

  async function load() {
    try {
      [shipments, carriers] = await Promise.all([api.shipments(showArchive), api.carriers()]);
      loaded = true;
    } catch (caught) {
      report(caught);
    }
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(
      async () => {
        now = new Date();
        if (meta && !meta.login_required && document.visibilityState === "visible") await load();
        schedulePoll();
      },
      pending ? PENDING_REFRESH_MS : REFRESH_MS,
    );
  }

  async function boot() {
    loaded = false;
    try {
      meta = await api.meta();
      const codes = meta.languages.map((language) => language.code);
      await i18n.load(meta.language ?? browserLanguage(codes));
      if (!meta.login_required) await load();
    } catch (caught) {
      // The language files could not be fetched either; say it in plain English.
      notify(caught instanceof ApiError && caught.code === "offline" ? "No connection to the server." : "Something went wrong.");
    }
    schedulePoll();
  }

  /** Text shared into the app from another one (Android share sheet). */
  function sharedText(): string {
    const params = new URLSearchParams(location.search);
    const shared = [params.get("url"), params.get("text"), params.get("title")]
      .filter(Boolean)
      .join(" ");
    if (shared) history.replaceState(null, "", location.pathname);
    return shared;
  }

  function replace(updated: Shipment) {
    const index = shipments.findIndex((s) => s.id === updated.id);
    if (index >= 0) shipments[index] = updated;
    else shipments.unshift(updated);
  }

  async function refresh(shipment: Shipment) {
    busy.push(shipment.id);
    // What would look different on the card if the carrier had news.
    const picture = (item: Shipment) =>
      JSON.stringify(item.parcels.map((p) => [p.status, p.description, p.eta_from, p.last_error?.code]));
    try {
      const updated = await api.refresh(shipment.id);
      notify(picture(updated) === picture(shipment) ? t("shipment.refresh_same") : t("shipment.refresh_new"));
      replace(updated);
    } catch (caught) {
      report(caught);
    } finally {
      busy = busy.filter((id) => id !== shipment.id);
    }
  }

  async function archive(shipment: Shipment, archived: boolean) {
    try {
      await api.archive(shipment.id, archived);
      shipments = shipments.filter((s) => s.id !== shipment.id);
      notify(archived ? t("shipment.archived_toast") : t("shipment.unarchive"), async () => {
        await api.archive(shipment.id, !archived).catch(report);
        toast = null;
        await load();
      });
    } catch (caught) {
      report(caught);
    }
  }

  async function remove(shipment: Shipment) {
    try {
      await api.remove(shipment.id);
      shipments = shipments.filter((s) => s.id !== shipment.id);
      notify(t("shipment.deleted_toast"));
    } catch (caught) {
      report(caught);
    }
  }

  async function toggleArchive() {
    showArchive = !showArchive;
    query = "";
    loaded = false;
    await load();
  }

  async function leave() {
    await api.logout().catch(() => undefined);
    settingsOpen = false;
    shipments = [];
    await boot();
  }

  onMount(() => {
    boot().then(() => {
      const shared = sharedText();
      if (shared && meta && !meta.login_required) editing = { shipment: null, prefill: shared };
    });
    const onVisible = () => {
      if (document.visibilityState === "visible" && meta && !meta.login_required) {
        now = new Date();
        load();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      clearTimeout(pollTimer);
    };
  });
</script>

<div class="wrap">
  <header class="top">
    <div class="brand">Parcel<span>Master</span></div>
    {#if meta && !meta.login_required}
      <button type="button" class="chip" aria-pressed={showArchive} onclick={toggleArchive}>
        {t("overview.archive")}
      </button>
      <button type="button" class="chip" onclick={() => (settingsOpen = true)}>
        {t("settings.title")}
      </button>
    {/if}
  </header>

  {#if meta?.demo}
    <p class="hint demo">
      <span>{t("demo.banner")}</span>
      <button type="button" class="chip" onclick={leave}>{t("demo.leave")}</button>
    </p>
  {/if}

  {#if !meta}
    <!-- Nothing to show until the server answered. -->
  {:else if meta.login_required}
    <div class="lede">
      <h1>{t("app.name")}</h1>
      <p class="muted">{t("app.tagline")}</p>
    </div>
    <Login demoAvailable={meta.demo_available} ondone={boot} />
  {:else}
    {#if !showArchive}
      <section class="lede">
        <p class="eyebrow">{t("overview.open")}</p>
        <p class="hero mono">{open.length}</p>
      </section>

      <dl class="stats">
        <div class="stat">
          <dt>{t("group.today")}</dt>
          <dd>{figures.today}</dd>
        </div>
        <div class="stat">
          <dt>{t("status.ready_for_pickup")}</dt>
          <dd>{figures.pickup}</dd>
        </div>
        <div class="stat">
          <dt>{t("status.problem")}</dt>
          <dd class:alert={figures.problem > 0}>{figures.problem}</dd>
        </div>
        <div class="stat">
          <dt>{t("group.done")}</dt>
          <dd>{figures.arrived}</dd>
        </div>
      </dl>
    {:else}
      <section class="lede">
        <p class="eyebrow">{t("overview.archive")}</p>
        <p class="hero mono">{shipments.length}</p>
      </section>
    {/if}

    <div class="row tools">
      <input
        type="search"
        class="search"
        placeholder={t("overview.search")}
        aria-label={t("overview.search")}
        bind:value={query}
      />
      {#if !showArchive}
        <button
          type="button"
          class="chip primary"
          onclick={() => {
            // Whoever typed a number into the search most likely wants to add it.
            editing = { shipment: null, prefill: query.trim() };
            query = "";
          }}
        >
          + {t("overview.add")}
        </button>
      {/if}
    </div>

    {#if loaded && shipments.length === 0}
      <section class="card empty">
        {#if showArchive}
          <p class="note">{t("overview.empty_archive")}</p>
        {:else}
          <h2>{t("overview.empty_title")}</h2>
          <p class="note">{t("overview.empty_text")}</p>
        {/if}
      </section>
    {:else if loaded && visible.length === 0}
      <p class="note">{t("overview.no_match", { query })}</p>
    {/if}

    {#snippet card(shipment: Shipment)}
      <ShipmentCard
        {shipment}
        {now}
        busy={busy.includes(shipment.id)}
        onedit={() => (editing = { shipment, prefill: "" })}
        onrefresh={() => refresh(shipment)}
        onarchive={(archived) => archive(shipment, archived)}
        ondelete={() => remove(shipment)}
      />
    {/snippet}

    {#if showArchive}
      {#each visible as shipment (shipment.id)}
        {@render card(shipment)}
      {/each}
    {:else}
      {#each groups as { group, items } (group)}
        <section class="group">
          <h2 class="eyebrow">
            {t(`group.${group}`)} <span class="count">{items.length}</span>
          </h2>
          {#each items as shipment (shipment.id)}
            {@render card(shipment)}
          {/each}
        </section>
      {/each}
    {/if}
  {/if}
</div>

{#if editing && meta}
  <ShipmentDialog
    shipment={editing.shipment}
    prefill={editing.prefill}
    {carriers}
    onsaved={(saved) => {
      replace(saved);
      editing = null;
      schedulePoll();
    }}
    onclose={() => (editing = null)}
  />
{/if}

{#if settingsOpen && meta}
  <SettingsDialog
    {meta}
    {now}
    onclose={() => (settingsOpen = false)}
    onchanged={() => load()}
    onlogout={leave}
  />
{/if}

{#if toast}
  <div class="toast" role="status">
    <span>{toast.text}</span>
    {#if toast.undo}
      <button type="button" class="chip" onclick={toast.undo}>{t("shipment.undo")}</button>
    {/if}
  </div>
{/if}

<style>
  .top {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: clamp(18px, 4vw, 28px);
  }

  .brand {
    font-family: var(--mono);
    font-weight: 600;
    font-size: 0.78rem;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    margin-right: auto;
  }

  .brand span {
    color: var(--sun);
  }

  .lede {
    margin-bottom: 20px;
  }

  .lede h1 {
    font-family: var(--mono);
    font-weight: 600;
    font-size: clamp(2rem, 7vw, 3rem);
    line-height: 1.05;
    letter-spacing: -0.02em;
    margin-bottom: 10px;
  }

  /* The one big number on the screen. */
  .hero {
    font-weight: 600;
    font-size: clamp(3.4rem, 14vw, 5.4rem);
    line-height: 0.95;
    letter-spacing: -0.02em;
  }

  .tools {
    margin-bottom: 28px;
    flex-wrap: nowrap;
  }

  .search {
    flex: 1 1 auto;
    min-width: 0;
  }

  .group {
    margin-bottom: 28px;
  }

  .group > h2 {
    margin-bottom: 10px;
    font-weight: 400;
  }

  .count {
    margin-left: 6px;
    color: var(--ink);
  }

  .empty h2 {
    font-size: 1.25rem;
    font-weight: 600;
    margin-bottom: 4px;
  }

  .demo {
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
  }

  .toast {
    position: fixed;
    left: 50%;
    bottom: max(20px, env(safe-area-inset-bottom));
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 12px;
    max-width: calc(100vw - 28px);
    padding: 10px 14px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-left: 3px solid var(--sun);
    border-radius: var(--r-hint);
    box-shadow: var(--shadow);
    font-size: 0.88rem;
    z-index: 10;
  }

  @media (max-width: 560px) {
    .tools {
      flex-wrap: wrap;
    }

    .search {
      flex-basis: 100%;
    }
  }
</style>
