<script lang="ts">
  import { progress } from "../lib/time";
  import type { Status } from "../lib/types";

  let { status }: { status: Status } = $props();

  const share = $derived(progress(status));
  // Gold is kept for arrival: the whole rail turns once the parcel is there.
  const arrived = $derived(status === "delivered");
</script>

<div class="rail" class:arrived aria-hidden="true">
  <div class="fill" style:width="{share * 100}%"></div>
  {#if share > 0 && share < 1}
    <div class="marker" style:left="{share * 100}%"></div>
  {/if}
</div>

<style>
  .rail {
    position: relative;
    height: 10px;
    border-radius: 999px;
    background: var(--surface-2);
    border: 1px solid var(--line);
    margin: 12px 0;
  }

  .fill {
    height: 100%;
    border-radius: 999px;
    background: var(--dusk);
    transition: width 0.3s ease;
  }

  .arrived .fill {
    background: var(--sun);
  }

  .marker {
    position: absolute;
    top: -4px;
    bottom: -4px;
    width: 2px;
    margin-left: -1px;
    background: var(--ink);
  }
</style>
