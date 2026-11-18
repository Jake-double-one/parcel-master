<script lang="ts">
  import { t } from "../lib/i18n.svelte";

  /** A longer explanation, kept behind a question mark until it is asked for. */
  let { text }: { text: string } = $props();
  let open = $state(false);
</script>

{#if text}
  <button
    type="button"
    class="ask"
    aria-expanded={open}
    aria-label={t("settings.help")}
    title={t("settings.help")}
    onclick={(event) => {
      event.preventDefault(); // inside a label this must not reach the field
      open = !open;
    }}>?</button
  >
  {#if open}<span class="answer">{text}</span>{/if}
{/if}

<style>
  .ask {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.5em;
    height: 1.5em;
    margin-left: 6px;
    padding: 0;
    font-family: var(--mono);
    font-size: 0.72rem;
    line-height: 1;
    letter-spacing: 0;
    color: var(--muted);
    background: none;
    border: 1px solid var(--line);
    border-radius: 50%;
    cursor: pointer;
    vertical-align: middle;
  }

  .ask:hover,
  .ask[aria-expanded="true"] {
    color: var(--ink);
    border-color: var(--dusk);
  }

  .ask[aria-expanded="true"] {
    background: var(--dusk-soft);
  }

  /* The label around it is small upper-case type; the answer is plain reading text. */
  .answer {
    display: block;
    margin: 8px 0 2px;
    font-family: var(--sans);
    font-size: 0.88rem;
    font-weight: 400;
    letter-spacing: normal;
    text-transform: none;
    line-height: 1.45;
    color: var(--muted);
  }
</style>
