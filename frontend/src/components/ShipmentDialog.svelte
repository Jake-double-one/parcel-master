<script lang="ts">
  import { onMount, untrack } from "svelte";
  import { api, ApiError } from "../lib/api";
  import { t, tOr } from "../lib/i18n.svelte";
  import { cameraAvailable, recognise } from "../lib/scanner";
  import type { Carrier, Direction, ParcelDraft, Shipment, ShipmentDraft } from "../lib/types";
  import Scanner from "./Scanner.svelte";

  /**
   * One dialog for both adding and editing: with a shipment it starts filled
   * in and saves changes, without one it creates.
   */
  let {
    shipment = null,
    carriers,
    prefill = "",
    onsaved,
    onclose,
  }: {
    shipment?: Shipment | null;
    carriers: Carrier[];
    prefill?: string;
    onsaved: (saved: Shipment) => void;
    onclose: () => void;
  } = $props();

  const DIRECTIONS: Direction[] = ["incoming", "outgoing", "return"];

  let dialog: HTMLDialogElement;
  // The form works on a copy; the shipment itself only changes after saving.
  const initial = untrack(() => shipment);
  let title = $state(initial?.title ?? "");
  let direction = $state<Direction>(initial?.direction ?? "incoming");
  let note = $state(initial?.note ?? "");
  let parcels = $state<ParcelDraft[]>(
    initial
      ? initial.parcels.map((p) => ({
          id: p.id,
          carrier: p.carrier,
          tracking_number: p.tracking_number,
          extra: { ...p.extra },
        }))
      : [emptyParcel()],
  );
  let hints = $state<Record<number, string>>({});
  let error = $state("");
  let saving = $state(false);
  let importing = $state(false);
  const canScan = cameraAvailable();
  /** Index of the parcel the camera is scanning for, or null. */
  let scanning = $state<number | null>(null);

  /** Take a scanned code if it holds a number or link one of the carriers knows. */
  async function scanned(text: string): Promise<boolean> {
    const index = scanning;
    if (index === null || !parcels[index]) return true;
    const found = await recognise(text);
    if (!found) return false;
    parcels[index].carrier = found.carrier;
    parcels[index].tracking_number = found.tracking_number;
    hints[index] = t("dialog.detected", { carrier: carrierOf(found.carrier)?.name ?? "" });
    applyDefaults();
    return true;
  }

  /** Trackers take a tracking number; a connected importer may take an order number. */
  function canAdd(carrier: Carrier): boolean {
    return carrier.kind === "tracker" || (carrier.by_reference && !!carrier.sync?.account);
  }
  const selectable = $derived(carriers.filter(canAdd));

  /** A parcel the importing service created: shown, but not edited by hand. */
  function isImported(parcel: ParcelDraft): boolean {
    return parcel.id !== undefined && carrierOf(parcel.carrier)?.kind === "importer";
  }

  /** A new entry that names an order of an importing service instead of a parcel. */
  function isOrder(parcel: ParcelDraft): boolean {
    return parcel.id === undefined && carrierOf(parcel.carrier)?.kind === "importer";
  }

  function emptyParcel(): ParcelDraft {
    const selectable = carriers.filter(canAdd);
    // With a single carrier there is nothing to choose.
    return { carrier: selectable.length === 1 ? selectable[0].id : "", tracking_number: "", extra: {} };
  }

  function carrierOf(id: string): Carrier | undefined {
    return carriers.find((c) => c.id === id);
  }

  /**
   * Defaults such as the user's own postal code only fit parcels coming to
   * them. They fill empty fields, and leave again when the direction changes.
   */
  function applyDefaults() {
    for (const parcel of parcels) {
      const defaults = carrierOf(parcel.carrier)?.parcel_defaults ?? {};
      for (const [key, value] of Object.entries(defaults)) {
        if (direction === "incoming" && !parcel.extra[key]) parcel.extra[key] = value;
        if (direction !== "incoming" && parcel.extra[key] === value) parcel.extra[key] = "";
      }
    }
  }

  function setDirection(option: Direction) {
    direction = option;
    applyDefaults();
  }

  /** Recognise carrier and number from whatever was typed or pasted: a number or a link. */
  async function detect(index: number, text: string) {
    const value = text.trim();
    if (value.length < 6) return;
    try {
      const { matches } = await api.detect(value);
      // The field may have changed while the server was thinking.
      if (parcels[index]?.tracking_number.trim() !== value) return;
      if (matches.length) {
        const [first, ...more] = matches;
        parcels[index].carrier = first.carrier;
        parcels[index].tracking_number = first.tracking_number;
        hints[index] = t("dialog.detected", { carrier: carrierOf(first.carrier)?.name ?? "" });
        applyDefaults();
        // A pasted message can hold several numbers.
        for (const match of more) {
          if (!parcels.some((p) => p.tracking_number === match.tracking_number)) {
            parcels.push({ carrier: match.carrier, tracking_number: match.tracking_number, extra: {} });
          }
        }
      } else if (!parcels[index].carrier) {
        hints[index] = t("dialog.not_detected");
      }
    } catch {
      // Recognition is a convenience; the carrier can always be chosen by hand.
    }
  }

  function removeParcel(index: number) {
    parcels.splice(index, 1);
    hints = {};
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    error = "";
    const draft: ShipmentDraft = {
      title,
      direction,
      note,
      parcels: parcels
        .filter((p) => p.tracking_number.trim())
        .map((p) => ({ ...p, tracking_number: p.tracking_number.trim() })),
    };
    if (!draft.parcels.length || draft.parcels.some((p) => !p.carrier)) {
      error = t("error.unknown_carrier");
      return;
    }
    const orders = draft.parcels.filter(isOrder);
    if (orders.length && (shipment || draft.parcels.length > 1)) {
      error = t("error.order_alone");
      return;
    }
    saving = true;
    importing = orders.length > 0;
    try {
      if (orders.length) {
        // The service fills in parcels, contents and status; name and note are the user's.
        let saved = await api.importOrder(orders[0].carrier, orders[0].tracking_number);
        if (title.trim() || note.trim() || direction !== saved.direction) {
          saved = await api.update(saved.id, {
            title: title.trim() || saved.title,
            direction,
            note,
            parcels: saved.parcels.map((p) => ({
              id: p.id,
              carrier: p.carrier,
              tracking_number: p.tracking_number,
              extra: p.extra,
            })),
          });
        }
        onsaved(saved);
      } else {
        onsaved(shipment ? await api.update(shipment.id, draft) : await api.create(draft));
      }
    } catch (caught) {
      const failure = caught instanceof ApiError ? caught : new ApiError(0, "generic");
      if (failure.code === "import_failed") {
        error = tOr(`carrier_error.${failure.details.reason}`, t("carrier_error.unavailable"));
      } else if (failure.code === "duplicate") {
        const existing = String(failure.details.title ?? "");
        const number = String(failure.details.tracking_number ?? "");
        error = existing
          ? t("error.duplicate", { number, title: existing })
          : t("error.duplicate_same", { number });
      } else {
        error = tOr(`error.${failure.code}`, t("error.generic"));
      }
    } finally {
      saving = false;
      importing = false;
    }
  }

  onMount(() => {
    dialog.showModal();
    if (!shipment) applyDefaults();
    if (prefill && !shipment) {
      parcels[0].tracking_number = prefill;
      detect(0, prefill);
    }
  });
</script>

<dialog bind:this={dialog} onclose={onclose} aria-labelledby="shipment-dialog-title">
  <form onsubmit={submit} novalidate>
    <div class="dialog-head">
      <h2 id="shipment-dialog-title">
        {shipment ? t("dialog.edit_title") : t("dialog.create_title")}
      </h2>
    </div>

    <div class="dialog-body">
      {#each parcels as parcel, index (index)}
        {@const carrier = carrierOf(parcel.carrier)}
        <fieldset class:nested={parcels.length > 1}>
          <legend class="eyebrow">
            {parcels.length > 1 ? t("dialog.parcel_n", { n: index + 1 }) : t("dialog.parcel")}
          </legend>

          {#if isImported(parcel)}
            <p class="imported">
              <span class="eyebrow">{carrier?.name}</span>
              <span class="mono">{parcel.tracking_number}</span>
            </p>
          {:else}
            <label class="field">
              <span class="eyebrow">
                {isOrder(parcel) ? t(`carrier.${parcel.carrier}.reference.label`) : t("dialog.paste")}
              </span>
              <span class="with-scan">
                <!-- svelte-ignore a11y_autofocus -->
                <input
                  type="text"
                  class="mono"
                  autocomplete="off"
                  autocapitalize="characters"
                  spellcheck="false"
                  autofocus={index === 0 && !shipment}
                  bind:value={parcel.tracking_number}
                  onpaste={(e) => {
                    const text = e.clipboardData?.getData("text") ?? "";
                    if (text.trim()) {
                      e.preventDefault();
                      parcel.tracking_number = text.trim();
                      detect(index, text);
                    }
                  }}
                  onblur={() => detect(index, parcel.tracking_number)}
                />
                {#if canScan}
                  <button
                    type="button"
                    class="scan"
                    aria-label={t("scanner.open")}
                    title={t("scanner.open")}
                    onclick={() => (scanning = index)}
                  >
                    <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true">
                      <path
                        d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h2v2h-2zM18 14h2v2h-2zM16 16h2v2h-2zM14 18h2v2h-2zM18 18h2v2h-2z"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="1.6"
                        stroke-linejoin="round"
                      />
                    </svg>
                  </button>
                {/if}
              </span>
              <span class="note">{hints[index] ?? (index === 0 ? t("dialog.paste_hint") : "")}</span>
            </label>

            {#if selectable.length > 0}
              <label class="field">
                <span class="eyebrow">{t("dialog.carrier")}</span>
                <select bind:value={parcel.carrier} onchange={applyDefaults}>
                  <option value="">{t("dialog.carrier_choose")}</option>
                  {#each selectable as option}
                    <option value={option.id}>
                      {option.name}{option.country ? ` (${option.country.toUpperCase()})` : ""}
                    </option>
                  {/each}
                </select>
              </label>
            {/if}

            {#if carrier && !carrier.configured}
              <p class="hint">{t("dialog.not_configured", { carrier: carrier.name })}</p>
            {/if}

            {#each carrier?.parcel_fields ?? [] as field}
              <label class="field">
                <span class="eyebrow">{t(`carrier.${carrier!.id}.parcel.${field.key}.label`)}</span>
                <input
                  type="text"
                  class="mono"
                  autocomplete="off"
                  placeholder={field.placeholder}
                  bind:value={parcel.extra[field.key]}
                />
                <span class="note">{tOr(`carrier.${carrier!.id}.parcel.${field.key}.help`, "")}</span>
              </label>
            {/each}
          {/if}

          {#if parcels.length > 1}
            <button type="button" class="chip" onclick={() => removeParcel(index)}>
              {t("dialog.remove_parcel")}
            </button>
          {/if}
        </fieldset>
      {/each}

      <div class="more">
        <button
          type="button"
          class="chip"
          onclick={() => {
            parcels.push(emptyParcel());
            applyDefaults();
          }}
        >
          + {t("dialog.add_parcel")}
        </button>
        <p class="note">{t("dialog.group_hint")}</p>
      </div>

      <label class="field">
        <span class="eyebrow">{t("dialog.name")}</span>
        <input type="text" maxlength="120" placeholder={t("dialog.name_placeholder")} bind:value={title} />
      </label>

      <div class="field" role="radiogroup" aria-labelledby="direction-label">
        <span class="eyebrow" id="direction-label">{t("dialog.direction")}</span>
        <div class="row">
          {#each DIRECTIONS as option}
            <button
              type="button"
              class="chip"
              role="radio"
              aria-checked={direction === option}
              onclick={() => setDirection(option)}
            >
              {t(`direction.${option}`)}
            </button>
          {/each}
        </div>
      </div>

      <label class="field last">
        <span class="eyebrow">{t("dialog.note")}</span>
        <textarea maxlength="2000" bind:value={note}></textarea>
      </label>

      {#if error}
        <p class="hint alert" role="alert">{error}</p>
      {/if}
    </div>

    <div class="dialog-foot">
      <button type="button" class="chip" onclick={() => dialog.close()}>{t("dialog.cancel")}</button>
      <button type="submit" class="chip primary" disabled={saving}>
        {importing ? t("dialog.importing") : shipment ? t("dialog.save") : t("dialog.create")}
      </button>
    </div>
  </form>
</dialog>

{#if scanning !== null}
  <Scanner onread={scanned} onclose={() => (scanning = null)} />
{/if}

<style>
  form {
    display: flex;
    flex-direction: column;
    min-height: 0;
    flex: 1;
  }

  fieldset {
    border: 0;
    margin: 0 0 6px;
    padding: 0;
    min-width: 0;
  }

  fieldset.nested {
    margin: 0 0 18px 6px;
    padding: 0 0 4px 18px;
    border-left: 2px solid var(--line);
  }

  legend {
    padding: 0;
    margin-bottom: 12px;
  }

  fieldset:not(.nested) legend {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }

  .imported {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 2px 12px;
    margin-bottom: 14px;
    overflow-wrap: anywhere;
  }

  .more {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 14px;
    margin-bottom: 22px;
  }

  .last {
    margin-bottom: 0;
  }

  .hint {
    margin-top: 16px;
    margin-bottom: 0;
  }

  fieldset .hint {
    margin: -8px 0 22px;
  }
</style>
