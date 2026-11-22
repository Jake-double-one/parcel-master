<script lang="ts">
  import Help from "./Help.svelte";
  import { onMount } from "svelte";
  import { api, ApiError, type LogReport } from "../lib/api";
  import { ago, until } from "../lib/format";
  import { i18n, t, tOr } from "../lib/i18n.svelte";
  import { install } from "../lib/install.svelte";
  import {
    disablePush,
    enablePush,
    pushEndpoint,
    pushState,
    syncPush,
    type PushState,
  } from "../lib/push";
  import { currentSkin, setSkin, SKINS } from "../lib/skins";
  import type { Carrier, Health, Meta, Settings, SystemStatus } from "../lib/types";

  let {
    meta,
    now,
    onclose,
    onchanged,
    onlogout,
  }: {
    meta: Meta;
    now: Date;
    onclose: () => void;
    /** Settings were saved; the page reloads what depends on them. */
    onchanged: (languageChanged: boolean) => void;
    onlogout: () => void;
  } = $props();

  const TABS = ["general", "carriers", "notifications", "status", "data"] as const;
  type Tab = (typeof TABS)[number];
  const EVENTS = [
    "out_for_delivery",
    "delivery_window",
    "stops",
    "ready_for_pickup",
    "delivered",
    "problem",
    "stale",
    "pickup_reminder",
    "any_change",
  ];

  let dialog: HTMLDialogElement;
  let tab = $state<Tab>("general");
  let settings = $state<Settings | null>(null);
  let carriers = $state<Carrier[]>([]);
  let status = $state<SystemStatus | null>(null);
  let appriseText = $state("");
  let skin = $state(currentSkin());
  let push = $state<PushState>("off");
  let saveState = $state<"idle" | "saving" | "saved">("idle");
  let testState = $state("");
  let deviceTest = $state("");
  let journal = $state<LogReport | null>(null);
  let copied = $state(false);

  async function loadJournal() {
    try {
      journal = await api.log();
    } catch (caught) {
      fail(caught);
    }
  }

  /** The moment as the viewer's clock shows it, with the offset so it stays unambiguous. */
  function localStamp(time: string): string {
    const at = new Date(time);
    if (Number.isNaN(at.getTime())) return time;
    const two = (n: number) => String(Math.trunc(Math.abs(n))).padStart(2, "0");
    const off = -at.getTimezoneOffset();
    return (
      `${at.getFullYear()}-${two(at.getMonth() + 1)}-${two(at.getDate())}` +
      `T${two(at.getHours())}:${two(at.getMinutes())}:${two(at.getSeconds())}` +
      `${off < 0 ? "-" : "+"}${two(off / 60)}:${two(off % 60)}`
    );
  }

  function journalText(): string {
    return (journal?.entries ?? [])
      .map(
        (entry) =>
          `${localStamp(entry.time)} ${entry.level.toUpperCase()} ${entry.source}: ${entry.message}`,
      )
      .join("\n");
  }

  async function copyJournal() {
    await navigator.clipboard.writeText(journalText());
    copied = true;
    setTimeout(() => (copied = false), 2000);
  }
  let importState = $state("");
  let error = $state("");
  let working = $state("");
  // Carrier sections start folded; the heading already tells whether one is set up.
  let unfolded = $state<string[]>([]);

  function toggleModule(id: string) {
    unfolded = unfolded.includes(id) ? unfolded.filter((x) => x !== id) : [...unfolded, id];
  }
  let drafts = $state<Record<string, Record<string, string>>>({});
  let connectError = $state<Record<string, { text: string; detail: string }>>({});

  async function load() {
    [settings, carriers, status] = await Promise.all([api.settings(), api.carriers(), api.status()]);
    appriseText = settings.notifications.apprise_urls.join("\n");
    prepareDrafts();
  }

  function fail(caught: unknown) {
    const code = caught instanceof ApiError ? caught.code : "generic";
    error = tOr(`error.${code}`, t("error.generic"));
  }

  /** "Working · dhl.de": how the module is doing, and which way it asks. */
  function healthLabel(id: string, health: Health, needsNothing: boolean): string {
    const state =
      needsNothing && health.state !== "failing" && health.state !== "ok"
        ? t("settings.no_settings")
        : t(`settings.health_${health.state}`);
    if (!health.source || health.state === "off") return state;
    return `${state} · ${tOr(`carrier.${id}.source.${health.source}`, health.source)}`;
  }

  /** The browser's own words help more here than "that did not work". */
  function failPush(caught: unknown) {
    if (caught instanceof ApiError) return fail(caught);
    // Brave turns off the push service Chromium relies on until the user allows it.
    const brave = "brave" in navigator;
    if (brave && caught instanceof Error && caught.name === "AbortError") {
      error = t("settings.push_failed_brave");
      return;
    }
    const reason = caught instanceof Error ? `${caught.name}: ${caught.message}` : String(caught);
    error = t("settings.push_failed", { reason });
  }

  async function save(): Promise<boolean> {
    if (!settings) return false;
    error = "";
    saveState = "saving";
    try {
      const languageBefore = i18n.language;
      settings.notifications.apprise_urls = appriseText
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      // An emptied number field means "use the carrier's default".
      for (const own of Object.values(settings.carriers)) {
        own.daily_budget ??= null;
        own.sync_minutes ??= null;
      }
      settings = await api.saveSettings(settings);
      appriseText = settings.notifications.apprise_urls.join("\n");
      const languageChanged = settings.language !== languageBefore;
      if (languageChanged) await i18n.load(settings.language);
      [carriers, status] = await Promise.all([api.carriers(), api.status()]);
      saveState = "saved";
      setTimeout(() => (saveState = "idle"), 1800);
      onchanged(languageChanged);
      return true;
    } catch (caught) {
      saveState = "idle";
      fail(caught);
      return false;
    }
  }

  /** Every importer that is not connected gets an empty sign-in form. */
  function prepareDrafts() {
    for (const carrier of carriers) {
      if (carrier.kind === "importer" && !carrier.sync?.account) drafts[carrier.id] ??= {};
    }
  }

  async function reloadCarriers() {
    carriers = await api.carriers().catch(() => carriers);
    prepareDrafts();
  }

  async function syncNow(id: string) {
    error = "";
    working = id;
    try {
      await api.syncImporter(id);
      onchanged(false);
    } catch (caught) {
      fail(caught);
    } finally {
      working = "";
      await reloadCarriers();
    }
  }

  /** Check the sign-in. The server keeps the credentials only if it works. */
  async function connect(id: string) {
    delete connectError[id];
    working = id;
    try {
      await api.connectImporter(id, drafts[id] ?? {});
      delete drafts[id]; // the password has no business staying in the page
      onchanged(false);
    } catch (caught) {
      const failure = caught instanceof ApiError ? caught : new ApiError(0, "generic");
      connectError[id] =
        failure.code === "sign_in"
          ? {
              text: tOr(`carrier_error.${failure.details.reason}`, t("carrier_error.unavailable")),
              detail: String(failure.details.message ?? ""),
            }
          : { text: tOr(`error.${failure.code}`, t("error.generic")), detail: "" };
    } finally {
      working = "";
      await reloadCarriers();
    }
  }

  async function disconnect(id: string) {
    error = "";
    working = id;
    try {
      await api.disconnectImporter(id);
    } catch (caught) {
      fail(caught);
    } finally {
      working = "";
      await reloadCarriers();
    }
  }

  async function togglePush() {
    error = "";
    deviceTest = "";
    try {
      push = push === "on" ? await disablePush() : await enablePush();
      status = await api.status();
    } catch (caught) {
      failPush(caught);
    }
  }

  async function testDevice() {
    error = "";
    deviceTest = "";
    try {
      const endpoint = await pushEndpoint();
      const report = endpoint ? await api.testNotification(endpoint) : null;
      if (!report?.push || report.push.error === "gone") {
        deviceTest = t("settings.push_test_gone");
        push = await pushState();
      } else if (report.push.ok) deviceTest = t("settings.push_test_sent");
      else deviceTest = t("settings.push_test_failed", { reason: report.push.error });
    } catch (caught) {
      failPush(caught);
    }
  }

  async function sendTest() {
    testState = "";
    if (!(await save())) return;
    try {
      const report = await api.testNotification();
      const parts: string[] = [];
      if (report.push) {
        parts.push(
          report.push.ok
            ? t("settings.test_push_ok", { sent: report.push.sent, total: report.push.subscriptions })
            : t("settings.test_push_failed", { reason: report.push.error }),
        );
      }
      if (report.apprise) {
        parts.push(
          report.apprise.ok
            ? t("settings.test_apprise_ok")
            : t("settings.test_apprise_failed", { reason: report.apprise.error || "?" }),
        );
      }
      testState = parts.length ? parts.join(" · ") : t("settings.test_none");
    } catch (caught) {
      fail(caught);
    }
  }

  async function exportData() {
    const blob = new Blob([JSON.stringify(await api.exportData(), null, 2)], {
      type: "application/json",
    });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `parcelmaster-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  async function importData(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    try {
      const result = await api.importData(JSON.parse(await file.text()));
      importState = t("settings.import_done", result);
      onchanged(false);
    } catch {
      importState = t("settings.import_failed");
    }
  }

  function pickSkin(id: string) {
    setSkin(id);
    skin = id;
  }

  onMount(() => {
    dialog.showModal();
    load().catch(fail);
    syncPush()
      .catch(() => pushState())
      .then((state) => (push = state));
  });
</script>

<dialog bind:this={dialog} onclose={onclose} aria-labelledby="settings-title">
  <div class="dialog-head">
    <h2 id="settings-title">{t("settings.title")}</h2>
    <button type="button" class="chip" onclick={() => dialog.close()}>{t("settings.close")}</button>
  </div>

  <div class="tabs row">
    {#each TABS as name}
      <button
        type="button"
        class="chip"
        aria-pressed={tab === name}
        onclick={() => (tab = name)}
      >
        {t(`settings.tab_${name}`)}
      </button>
    {/each}
  </div>

  <div class="dialog-body">
    {#if !settings}
      <p class="note">…</p>
    {:else if tab === "general"}
      <label class="field">
        <span class="eyebrow">{t("settings.language")} <Help text={t("settings.language_help")} /></span>
        <select bind:value={settings.language}>
          {#each meta.languages as language}
            <option value={language.code}>{language.name}</option>
          {/each}
        </select>
      </label>

      <div class="field">
        <span class="eyebrow">{t("settings.app")}</span>
        {#if install.state === "ready"}
          <div class="row">
            <button type="button" class="chip primary" onclick={() => install.run()}>
              {t("settings.app_install")}
            </button>
          </div>
        {/if}
        <span class="note">{t(`settings.app_${install.state}`)}</span>
      </div>

      <div class="field" role="radiogroup" aria-labelledby="skin-label">
        <span class="eyebrow" id="skin-label">{t("settings.skin")}</span>
        <div class="skins">
          {#each SKINS as option}
            <button
              type="button"
              role="radio"
              class="skin"
              aria-checked={skin === option.id}
              onclick={() => pickSkin(option.id)}
            >
              <span class="swatch" data-skin={option.id} aria-hidden="true"></span>
              <span class="micro name">{i18n.language === "de" ? option.nameDe : option.name}</span>
            </button>
          {/each}
        </div>
      </div>

      <label class="field">
        <span class="eyebrow">{t("settings.interval")} <Help text={t("settings.interval_help")} /></span>
        <span class="inline-number">
          <input type="number" min="15" max="1440" step="5" bind:value={settings.polling.interval_minutes} />
          <span class="mono muted">{t("settings.interval_unit")}</span>
        </span>
      </label>

      <div class="field">
        <span class="eyebrow">{t("settings.saver")} <Help text={t("settings.saver_help")} /></span>
        <div class="row">
          <button
            type="button"
            class="chip"
            aria-pressed={settings.polling.saver.enabled}
            onclick={() => (settings!.polling.saver.enabled = !settings!.polling.saver.enabled)}
          >
            {settings.polling.saver.enabled ? t("settings.saver_on") : t("settings.saver_off")}
          </button>
          <label class="time">
            <span class="micro">{t("settings.saver_night")}</span>
            <input type="time" bind:value={settings.polling.saver.from} />
          </label>
          <label class="time">
            <span class="micro">{t("settings.quiet_to")}</span>
            <input type="time" bind:value={settings.polling.saver.to} />
          </label>
        </div>
      </div>

      <label class="field">
        <span class="eyebrow">{t("settings.stale_days")}</span>
        <span class="inline-number">
          <input type="number" min="1" max="30" bind:value={settings.polling.stale_days} />
          <span class="mono muted">{t("settings.days")}</span>
        </span>
      </label>

      <label class="field">
        <span class="eyebrow">{t("settings.auto_archive")} <Help text={t("settings.auto_archive_help")} /></span>
        <span class="inline-number">
          <input type="number" min="0" max="90" bind:value={settings.polling.auto_archive_days} />
          <span class="mono muted">{t("settings.days")}</span>
        </span>
      </label>

      <label class="field last">
        <span class="eyebrow">{t("settings.timezone")}</span>
        <input type="text" class="mono" bind:value={settings.timezone} />
      </label>
    {:else if tab === "carriers"}
      {#if carriers.some((carrier) => carrier.companion)}
        <div class="field">
          <span class="eyebrow">
            {t("settings.companion")}
            <Help text={t("settings.companion_help")} />
          </span>
          {#each carriers.filter((carrier) => carrier.companion) as carrier}
            <div class="row companion">
              <a class="chip" href={carrier.companion} target="_blank" rel="noopener">
                {t("settings.companion_install", { carrier: carrier.name })} ↗
              </a>
              <Help text={tOr(`carrier.${carrier.id}.companion.help`, "")} />
            </div>
          {/each}
          <label class="field discovery">
            <span class="eyebrow">
              {t("settings.discovery")}
              <Help text={t("settings.discovery_help")} />
            </span>
            <select bind:value={settings.discovery.mode}>
              <option value="button">{t("settings.discovery_button")}</option>
              <option value="suggest">{t("settings.discovery_suggest")}</option>
              <option value="auto">{t("settings.discovery_auto")}</option>
            </select>
          </label>
        </div>
      {/if}
      {#each carriers as carrier}
        {@const own = settings.carriers[carrier.id] ?? {}}
        {@const ready = carrier.kind === "importer" ? !!carrier.sync?.account : carrier.configured}
        {@const open = unfolded.includes(carrier.id)}
        <section class="module" class:folded={!open}>
          <!-- The heading stays visible with its state; the rest folds away. -->
          <button
            type="button"
            class="row module-head fold-toggle"
            aria-expanded={open}
            onclick={() => toggleModule(carrier.id)}
          >
            <svg class="chev" class:open width="8" height="10" viewBox="0 0 8 10" aria-hidden="true">
              <path d="M1 1l5 4-5 4" fill="none" stroke="currentColor" stroke-width="1.6" />
            </svg>
            <h3>{carrier.name}{carrier.country ? ` · ${carrier.country.toUpperCase()}` : ""}</h3>
            <span
              class="micro state"
              class:ready={carrier.health.state === "ok"}
              class:failing={carrier.health.state === "failing"}
            >
              {healthLabel(carrier.id, carrier.health, carrier.settings_fields.length === 0)}
            </span>
          </button>
          {#if open}
          {#if carrier.kind === "importer"}
            {@const account = carrier.sync?.account}
            {#if account}
              <!-- Connected: the form is folded away until the user logs out. -->
              <p class="account">
                <span>{t("settings.connected")}</span>
                <span class="mono">{account}</span>
              </p>
              <div class="row sync">
                <button
                  type="button"
                  class="chip"
                  disabled={working === carrier.id}
                  onclick={() => syncNow(carrier.id)}
                >
                  {working === carrier.id ? t("settings.syncing") : t("settings.sync_now")}
                </button>
                <button
                  type="button"
                  class="chip"
                  disabled={working === carrier.id}
                  onclick={() => disconnect(carrier.id)}
                >
                  {t("settings.disconnect")}
                </button>
              </div>
              <label class="field">
                <span class="eyebrow">{t("settings.sync_every")} <Help text={t("settings.sync_every_help")} /></span>
                <span class="inline-number">
                  <input
                    type="number"
                    min="30"
                    max="1440"
                    step="15"
                    placeholder={String(carrier.sync?.default_interval_minutes ?? "")}
                    bind:value={own.sync_minutes}
                  />
                  <span class="mono muted">{t("settings.interval_unit")}</span>
                </span>
              </label>
              <p class="note sync">
                {carrier.sync?.last_sync_at
                  ? t("settings.sync_last", {
                      time: ago(carrier.sync.last_sync_at, now),
                      count: carrier.sync.found ?? 0,
                    })
                  : t("settings.sync_never")}
              </p>
              {#if carrier.sync?.last_error}
                <p class="hint alert">
                  <span>
                    {tOr(`carrier_error.${carrier.sync.last_error.code}`, t("carrier_error.unavailable"))}
                    {#if carrier.sync.last_error.detail}
                      <span class="detail mono">{carrier.sync.last_error.detail}</span>
                    {/if}
                  </span>
                </p>
              {/if}
            {:else if drafts[carrier.id]}
              {@const draft = drafts[carrier.id]}
              <form
                onsubmit={(event) => {
                  event.preventDefault();
                  connect(carrier.id);
                }}
              >
                {#each carrier.settings_fields as field}
                  <label class="field">
                    <span class="eyebrow">{t(`carrier.${carrier.id}.settings.${field.key}.label`)} <Help text={tOr(`carrier.${carrier.id}.settings.${field.key}.help`, "")} /></span>
                    <input
                      type={field.type === "secret" ? "password" : "text"}
                      class="mono"
                      autocomplete="off"
                      placeholder={field.placeholder}
                      disabled={working === carrier.id}
                      bind:value={draft[field.key]}
                    />
                  </label>
                {/each}
                <div class="row sync">
                  <button type="submit" class="chip primary" disabled={working === carrier.id}>
                    {working === carrier.id ? t("settings.connecting") : t("settings.connect")}
                  </button>
                  <span class="note">{t("settings.connect_help")}</span>
                </div>
              </form>
              {#if connectError[carrier.id]}
                <p class="hint alert" role="alert">
                  <span>
                    {connectError[carrier.id].text}
                    {#if connectError[carrier.id].detail}
                      <span class="detail mono">{connectError[carrier.id].detail}</span>
                    {/if}
                  </span>
                </p>
              {/if}
            {/if}
          {:else}
            {#each carrier.settings_fields as field}
              <label class="field">
                <span class="eyebrow">{t(`carrier.${carrier.id}.settings.${field.key}.label`)} <Help text={tOr(`carrier.${carrier.id}.settings.${field.key}.help`, "")} /></span>
                {#if field.type === "choice"}
                  <select
                    value={own[field.key] || field.options[0]}
                    onchange={(event) => (own[field.key] = event.currentTarget.value)}
                  >
                    {#each field.options as option}
                      <option value={option}>
                        {tOr(`carrier.${carrier.id}.settings.${field.key}.option.${option}`, option)}
                      </option>
                    {/each}
                  </select>
                {:else}
                  <input
                    type={field.type === "secret" ? "password" : "text"}
                    class="mono"
                    autocomplete="off"
                    placeholder={field.placeholder}
                    bind:value={own[field.key]}
                  />
                {/if}
              </label>
            {/each}
            <label class="field">
              <span class="eyebrow">{t("settings.budget")}</span>
              <span class="inline-number">
                <input
                  type="number"
                  min="1"
                  placeholder={carrier.default_daily_budget ? String(carrier.default_daily_budget) : "∞"}
                  bind:value={own.daily_budget}
                />
              </span>
              <span class="note">
                {carrier.daily_budget
                  ? t("settings.budget_help", { used: carrier.used_today, budget: carrier.daily_budget })
                  : t("settings.budget_unlimited", { used: carrier.used_today })}
              </span>
            </label>
          {/if}
          {/if}
        </section>
      {/each}
    {:else if tab === "notifications"}
      {#if meta.demo}
        <p class="hint">{t("error.not_in_demo")}</p>
      {:else}
        <div class="field">
          <span class="eyebrow">{t("settings.push")} <Help text={t("settings.push_help")} /></span>
          <div class="row">
            <button
              type="button"
              class="chip main"
              aria-pressed={push === "on"}
              disabled={push === "unsupported" || push === "blocked"}
              onclick={togglePush}
            >
              {push === "on" ? t("settings.push_on") : t("settings.push_off")}
            </button>
            <button type="button" class="chip" disabled={push !== "on"} onclick={testDevice}>
              {t("settings.push_test")}
            </button>
          </div>
          {#if deviceTest}
            <span class="note" role="status">{deviceTest}</span>
          {/if}
          {#if push === "blocked"}
            <span class="note">{t("settings.push_blocked")}</span>
          {:else if push === "unsupported"}
            <span class="note">{t("settings.push_unsupported")}</span>
          {:else}
            <span class="note">
              {#if status}
                {status.push_subscriptions === 0
                  ? t("settings.push_devices_0")
                  : status.push_subscriptions === 1
                    ? t("settings.push_devices_1")
                    : t("settings.push_devices_n", { count: status.push_subscriptions })}
              {/if}
            </span>
          {/if}
        </div>

        <label class="field">
          <span class="eyebrow">{t("settings.apprise")} <Help text={t("settings.apprise_help")} /></span>
          <textarea class="mono" spellcheck="false" bind:value={appriseText}></textarea>
        </label>
      {/if}

      <div class="field">
        <span class="eyebrow">{t("settings.events")}</span>
        <div class="row">
          {#each EVENTS as event}
            <button
              type="button"
              class="chip"
              aria-pressed={settings.notifications.events[event]}
              onclick={() =>
                (settings!.notifications.events[event] = !settings!.notifications.events[event])}
            >
              {t(`settings.event_${event}`)}
            </button>
          {/each}
        </div>
      </div>

      <div class="field">
        <span class="eyebrow">{t("settings.quiet")} <Help text={t("settings.quiet_help")} /></span>
        <div class="row">
          <button
            type="button"
            class="chip"
            aria-pressed={settings.notifications.quiet_hours.enabled}
            onclick={() =>
              (settings!.notifications.quiet_hours.enabled =
                !settings!.notifications.quiet_hours.enabled)}
          >
            {settings.notifications.quiet_hours.enabled ? t("settings.quiet_on") : t("settings.quiet_off")}
          </button>
          <label class="time">
            <span class="micro">{t("settings.quiet_from")}</span>
            <input type="time" bind:value={settings.notifications.quiet_hours.from} />
          </label>
          <label class="time">
            <span class="micro">{t("settings.quiet_to")}</span>
            <input type="time" bind:value={settings.notifications.quiet_hours.to} />
          </label>
        </div>
      </div>

      {#if !meta.demo}
        <div class="row last">
          <button type="button" class="chip" onclick={sendTest}>{t("settings.test")}</button>
          <span class="note" role="status">{testState}</span>
        </div>
      {/if}
    {:else if tab === "status" && status}
      <p class="hint" class:alert={!status.scheduler.running && !meta.demo}>
        <span>
          {status.scheduler.running ? t("settings.scheduler_running") : t("settings.scheduler_stopped")}
          {#if status.scheduler.last_run}
            {t("settings.scheduler_last", { time: ago(status.scheduler.last_run, now) })}
          {/if}
          {#if settings.polling.saver.enabled}
            {t("settings.saver_status", {
              from: settings.polling.saver.from,
              to: settings.polling.saver.to,
            })}
          {/if}
        </span>
      </p>
      {#each status.modules as module}
        <section class="module">
          <div class="row module-head">
            <h3>{module.name}</h3>
            <span
              class="micro"
              class:ready={module.health.state === "ok"}
              class:failing={module.health.state === "failing"}
            >
              {healthLabel(module.id, module.health, false)}
            </span>
          </div>
          <dl class="stats">
            <div class="stat">
              <dt>{t("overview.open")}</dt>
              <dd>{module.open_parcels}</dd>
            </div>
            <div class="stat">
              <dt>{t("settings.budget")}</dt>
              <dd>{module.used_today}{module.daily_budget ? `/${module.daily_budget}` : ""}</dd>
            </div>
          </dl>
          <p class="note">
            {#if module.kind === "importer" && module.sync}
              {t("settings.pace_importer", { minutes: module.sync.interval_minutes })}
              {#if module.sync.next_sync_at}
                {t("settings.next_sync", { time: until(module.sync.next_sync_at, now) })}
              {:else}
                {t("settings.no_next_sync")}
              {/if}
            {:else}
              {t("settings.pace_tracker", {
                minutes: settings.polling.interval_minutes,
                fast: Math.max(15, Math.floor(settings.polling.interval_minutes / 4)),
              })}
            {/if}
          </p>
          <p class="note">
            {module.last_success_at
              ? t("settings.module_last_success", { time: ago(module.last_success_at, now) })
              : t("settings.module_never")}
          </p>
          {#if module.last_error}
            <p class="hint alert">
              {t("settings.module_failing", {
                count: module.failing_parcels,
                error: tOr(`carrier_error.${module.last_error.code}`, module.last_error.code),
              })}
            </p>
          {/if}
        </section>
      {/each}

      {#if !meta.demo}
        <details class="journal" ontoggle={(event) => event.currentTarget.open && loadJournal()}>
          <summary>
            <svg class="chev" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
              <path d="M3 1l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6" />
            </svg>
            {t("settings.log")}
          </summary>
          {#if journal}
            <p class="note">{t("settings.log_help", { level: journal.level })}</p>
            <div class="row">
              <button type="button" class="chip" onclick={loadJournal}>{t("settings.log_reload")}</button>
              <button type="button" class="chip" onclick={copyJournal} disabled={!journal.entries.length}>
                {copied ? t("settings.log_copied") : t("settings.log_copy")}
              </button>
            </div>
            {#if journal.entries.length}
              <ol class="lines">
                {#each journal.entries as entry}
                  <li class:warn={entry.level === "warning"} class:bad={entry.level === "error"}>
                    <span class="when">{new Date(entry.time).toLocaleTimeString()}</span>
                    <span class="what">{entry.source}: {entry.message}</span>
                  </li>
                {/each}
              </ol>
            {:else}
              <p class="note">{t("settings.log_empty")}</p>
            {/if}
          {/if}
        </details>
      {/if}
    {:else if tab === "data"}
      <div class="row">
        <button type="button" class="chip" onclick={exportData}>{t("settings.export")}</button>
        <label class="chip">
          {t("settings.import")}
          <input type="file" accept="application/json,.json" class="visually-hidden" onchange={importData} />
        </label>
      </div>
      <p class="note gap" role="status">{importState}</p>
      <p class="micro gap">{t("settings.version", { version: meta.version })}</p>
      {#if meta.password_set && !meta.demo}
        <div class="row gap">
          <button type="button" class="chip" onclick={onlogout}>{t("settings.logout")}</button>
        </div>
      {/if}
    {/if}

    {#if error}
      <p class="hint alert bottom" role="alert">{error}</p>
    {/if}
  </div>

  {#if tab === "general" || tab === "carriers" || tab === "notifications"}
    <div class="dialog-foot">
      <button type="button" class="chip primary" disabled={saveState === "saving"} onclick={save}>
        {saveState === "saved" ? t("settings.saved") : t("settings.save")}
      </button>
    </div>
  {/if}
</dialog>

<style>
  .companion + .companion {
    margin-top: 8px;
  }

  /* Its own question, set apart from the install buttons above. */
  .discovery {
    margin-top: 22px;
  }

  .journal {
    margin-top: 22px;
  }

  .journal .note {
    margin: 10px 0;
  }

  .lines {
    list-style: none;
    margin: 12px 0 0;
    padding: 10px 12px;
    max-height: 50vh;
    overflow: auto;
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: var(--r-field);
    font-family: var(--mono);
    font-size: 0.72rem;
  }

  .lines li {
    display: flex;
    gap: 10px;
    padding: 3px 0;
  }

  .lines .when {
    flex: 0 0 auto;
    color: var(--muted);
  }

  .lines .what {
    overflow-wrap: anywhere;
  }

  .lines .warn .what {
    color: var(--warn);
  }

  .lines .bad .what {
    color: var(--alert);
  }

  .tabs {
    padding: 12px clamp(18px, 4vw, 28px);
    border-bottom: 1px solid var(--line);
    /* Wrap on narrow screens; a scrolling row would bring scrollbars with it. */
    flex-wrap: wrap;
    flex: 0 0 auto;
  }

  .skins {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
    gap: 10px;
  }

  .skin {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
    padding: 10px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--r-field);
    cursor: pointer;
  }

  .skin[aria-checked="true"] {
    border-color: var(--dusk);
    box-shadow: inset 0 0 0 1px var(--dusk);
  }

  .skin .name {
    color: var(--ink);
  }

  /* One square per skin, split diagonally: its background and its violet. */
  .swatch {
    display: block;
    width: 100%;
    height: 34px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: linear-gradient(to top left, var(--dusk) 0 50%, var(--bg) 50% 100%);
  }

  .module {
    padding-bottom: 6px;
    margin-bottom: 22px;
    border-bottom: 1px solid var(--line);
  }

  .module:last-of-type {
    border-bottom: 0;
    margin-bottom: 0;
  }

  .module-head {
    justify-content: space-between;
    margin-bottom: 14px;
  }

  h3 {
    font-family: var(--mono);
    font-size: 0.9rem;
    font-weight: 600;
    letter-spacing: 0.04em;
  }

  .micro.ready {
    color: var(--ok);
  }

  .micro.failing {
    color: var(--warn);
  }

  .fold-toggle {
    width: 100%;
    padding: 0;
    border: 0;
    background: none;
    cursor: pointer;
    text-align: left;
    justify-content: flex-start;
    gap: 10px;
    min-height: 36px;
  }

  .fold-toggle .state {
    margin-left: auto;
  }

  .fold-toggle .chev {
    flex: 0 0 auto;
    color: var(--muted);
    transition: transform 0.15s ease;
  }

  .fold-toggle .chev.open {
    transform: rotate(90deg);
  }

  .module.folded {
    padding-bottom: 0;
    margin-bottom: 8px;
  }

  .module.folded .module-head {
    margin-bottom: 8px;
  }

  .module .stats {
    grid-template-columns: repeat(2, 1fr);
    margin-bottom: 10px;
  }

  .sync {
    margin-bottom: 16px;
  }

  .account {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 2px 10px;
    margin-bottom: 14px;
    overflow-wrap: anywhere;
  }

  .detail {
    display: block;
    margin-top: 6px;
    font-size: 0.74rem;
    overflow-wrap: anywhere;
  }

  .time {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .time input {
    width: auto;
  }

  .last {
    margin-bottom: 0;
  }

  .gap {
    margin-top: 16px;
  }

  .bottom {
    margin: 18px 0 0;
  }
</style>
