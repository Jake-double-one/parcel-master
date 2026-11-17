<script lang="ts">
  import { onMount } from "svelte";
  import { t } from "../lib/i18n.svelte";
  import { createDetector, type Detector } from "../lib/scanner";

  /**
   * Shows the camera and reports what it reads. `onread` decides whether a
   * code is the one wanted (a label carries several) by returning true.
   */
  let {
    onread,
    onclose,
  }: {
    onread: (text: string) => Promise<boolean>;
    onclose: () => void;
  } = $props();

  let dialog: HTMLDialogElement;
  let video = $state<HTMLVideoElement>()!;
  let stream: MediaStream | null = null;
  let stopped = false;
  let problem = $state("");
  let last = $state("");
  const rejected = new Set<string>();

  async function start() {
    let detector: Detector;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" } },
        audio: false,
      });
      video.srcObject = stream;
      await video.play();
      detector = await createDetector();
    } catch (caught) {
      const name = caught instanceof DOMException ? caught.name : "";
      problem =
        name === "NotAllowedError" || name === "SecurityError"
          ? t("scanner.blocked")
          : name === "NotFoundError" || name === "OverconstrainedError"
            ? t("scanner.no_camera")
            : t("scanner.failed");
      return;
    }
    while (!stopped) {
      try {
        for (const code of await detector.detect(video)) {
          const text = code.rawValue.trim();
          if (!text || rejected.has(text)) continue;
          if (await onread(text)) {
            close();
            return;
          }
          // Not a tracking number or link we know: remember it and keep looking.
          rejected.add(text);
          last = text;
        }
      } catch {
        // A frame that cannot be read is simply skipped.
      }
      await new Promise((resolve) => setTimeout(resolve, 180));
    }
  }

  function release() {
    stopped = true;
    stream?.getTracks().forEach((track) => track.stop());
  }

  function close() {
    release();
    dialog.close();
  }

  onMount(() => {
    dialog.showModal();
    start();
    return release;
  });
</script>

<dialog bind:this={dialog} onclose={() => (release(), onclose())} aria-labelledby="scanner-title">
  <div class="dialog-head">
    <h2 id="scanner-title">{t("scanner.title")}</h2>
    <button type="button" class="chip" onclick={close}>{t("settings.close")}</button>
  </div>
  <div class="dialog-body">
    {#if problem}
      <p class="hint alert" role="alert">{problem}</p>
    {/if}
    <!-- The video stays in the page even after a failure: the camera start refers to it. -->
    <!-- svelte-ignore a11y_media_has_caption -->
    <video bind:this={video} class:visually-hidden={!!problem} playsinline muted></video>
    {#if !problem}
      <p class="note">{t("scanner.hint")}</p>
      {#if last}
        <p class="note unknown">
          {t("scanner.unknown")} <span class="mono">{last.slice(0, 60)}</span>
        </p>
      {/if}
    {/if}
  </div>
</dialog>

<style>
  video {
    display: block;
    width: 100%;
    max-height: 60dvh;
    object-fit: cover;
    background: var(--surface-2);
    border: 1px solid var(--line);
    border-radius: var(--r-field);
    margin-bottom: 12px;
  }

  .unknown {
    margin-top: 8px;
    overflow-wrap: anywhere;
  }

  .hint {
    margin-bottom: 0;
  }
</style>
