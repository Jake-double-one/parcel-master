<script lang="ts">
  import { api, ApiError } from "../lib/api";
  import { i18n, t, tOr } from "../lib/i18n.svelte";

  let { demoAvailable, ondone }: { demoAvailable: boolean; ondone: () => void } = $props();

  let password = $state("");
  let error = $state("");
  let busy = $state(false);

  async function run(action: () => Promise<unknown>) {
    error = "";
    busy = true;
    try {
      await action();
      ondone();
    } catch (caught) {
      const code = caught instanceof ApiError ? caught.code : "generic";
      error = tOr(`error.${code}`, t("error.generic"));
    } finally {
      busy = false;
    }
  }
</script>

<section class="card login">
  <form
    onsubmit={(e) => {
      e.preventDefault();
      run(() => api.login(password));
    }}
  >
    <label class="field">
      <span class="eyebrow">{t("login.password")}</span>
      <input type="password" autocomplete="current-password" bind:value={password} />
    </label>
    {#if error}
      <p class="hint alert" role="alert">{error}</p>
    {/if}
    <button type="submit" class="chip primary" disabled={busy || !password}>
      {t("login.submit")}
    </button>
  </form>
</section>

{#if demoAvailable}
  <section class="demo">
    <button type="button" class="chip" disabled={busy} onclick={() => run(() => api.startDemo(i18n.language))}>
      {t("login.demo")}
    </button>
    <p class="note">{t("login.demo_hint")}</p>
  </section>
{/if}

<style>
  .login {
    max-width: 420px;
    margin: 0 auto 16px;
  }

  .demo {
    max-width: 420px;
    margin: 28px auto 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }
</style>
