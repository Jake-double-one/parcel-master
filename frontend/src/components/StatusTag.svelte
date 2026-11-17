<script lang="ts">
  import { t } from "../lib/i18n.svelte";
  import type { Status } from "../lib/types";

  let { status }: { status: Status } = $props();
</script>

<span class="tag {status}">{t(`status.${status}`)}</span>

<style>
  .tag {
    flex: 0 0 auto;
    font-family: var(--mono);
    font-size: 0.68rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 3px 10px;
    white-space: nowrap;
  }

  .in_transit,
  .out_for_delivery,
  .ready_for_pickup {
    color: var(--ink);
    border-color: var(--dusk);
  }

  .out_for_delivery,
  .ready_for_pickup {
    background: var(--dusk-soft);
  }

  .delivered {
    color: var(--on-sun);
    background: var(--sun);
    border-color: var(--sun);
  }

  .problem {
    color: var(--alert);
    border-color: var(--alert);
  }
</style>
