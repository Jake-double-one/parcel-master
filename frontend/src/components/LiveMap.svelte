<script lang="ts">
  import { t } from "../lib/i18n.svelte";

  // Where the van is and where it is headed, on a map made of OpenStreetMap tiles.
  let { van, home }: { van: [number, number]; home: [number, number] | null } = $props();

  const TILE = 256;
  const HEIGHT = 200;
  const MAX_ZOOM = 17;
  let width = $state(320);

  /** Position on the world map at a zoom level, in pixels. */
  function world([latitude, longitude]: [number, number], zoom: number): [number, number] {
    const scale = TILE * 2 ** zoom;
    const rad = (latitude * Math.PI) / 180;
    return [
      ((longitude + 180) / 360) * scale,
      ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * scale,
    ];
  }

  const view = $derived.by(() => {
    // The closest zoom at which both places fit with some room around them.
    let zoom = MAX_ZOOM;
    if (home) {
      for (; zoom > 3; zoom--) {
        const [ax, ay] = world(van, zoom);
        const [bx, by] = world(home, zoom);
        if (Math.abs(ax - bx) <= width - 90 && Math.abs(ay - by) <= HEIGHT - 90) break;
      }
    }
    const a = world(van, zoom);
    const b = home ? world(home, zoom) : a;
    const left = (a[0] + b[0]) / 2 - width / 2;
    const top = (a[1] + b[1]) / 2 - HEIGHT / 2;
    const tiles: { src: string; x: number; y: number }[] = [];
    const count = 2 ** zoom;
    for (let tx = Math.floor(left / TILE); tx * TILE < left + width; tx++) {
      for (let ty = Math.floor(top / TILE); ty * TILE < top + HEIGHT; ty++) {
        if (ty < 0 || ty >= count) continue;
        const wrapped = ((tx % count) + count) % count;
        tiles.push({
          src: `https://tile.openstreetmap.org/${zoom}/${wrapped}/${ty}.png`,
          x: tx * TILE - left,
          y: ty * TILE - top,
        });
      }
    }
    return {
      tiles,
      van: [a[0] - left, a[1] - top],
      home: home ? [b[0] - left, b[1] - top] : null,
    };
  });
</script>

<div class="map" bind:clientWidth={width} role="img" aria-label={t("shipment.map_label")}>
  {#each view.tiles as tile (tile.src)}
    <img src={tile.src} alt="" style="left:{tile.x}px;top:{tile.y}px" draggable="false" />
  {/each}
  {#if view.home}
    <span class="pin home" style="left:{view.home[0]}px;top:{view.home[1]}px" title={t("shipment.map_home")}>
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <path d="M4 11 12 4l8 7v9h-5v-6H9v6H4z" fill="currentColor" />
      </svg>
    </span>
  {/if}
  <span class="pin van" style="left:{view.van[0]}px;top:{view.van[1]}px" title={t("shipment.map_van")}>
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
      <path
        d="M2 7h11v9H2zM13 10h4l4 3v3h-8zM6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM17 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"
        fill="currentColor"
      />
    </svg>
  </span>
  <a class="credit" href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">
    © OpenStreetMap
  </a>
</div>

<style>
  .map {
    position: relative;
    height: 200px;
    margin-top: 12px;
    border: 1px solid var(--line);
    border-radius: 12px;
    overflow: hidden;
    background: var(--surface-2);
  }

  img {
    position: absolute;
    width: 256px;
    height: 256px;
    max-width: none;
    user-select: none;
  }

  .pin {
    position: absolute;
    display: grid;
    place-items: center;
    width: 30px;
    height: 30px;
    margin: -15px 0 0 -15px;
    border-radius: 50%;
    border: 2px solid #fff;
    color: #fff;
    box-shadow: 0 1px 4px rgb(0 0 0 / 40%);
  }

  .pin.van {
    background: #1d6b4f;
  }

  .pin.home {
    background: #b3261e;
  }

  .credit {
    position: absolute;
    right: 0;
    bottom: 0;
    padding: 1px 6px;
    font-size: 0.65rem;
    color: #333;
    background: rgb(255 255 255 / 80%);
    text-decoration: none;
    border-top-left-radius: 6px;
  }
</style>
