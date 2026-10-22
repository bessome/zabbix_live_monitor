(() => {
  const root = document.querySelector("[data-modem-status-url]");
  if (!root) return;
  const value = root.querySelector("strong");
  let inFlight = false;
  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 40000);
    try {
      const response = await fetch(root.dataset.modemStatusUrl, {
        credentials: "same-origin", cache: "no-store", signal: controller.signal
      });
      if (response.status === 401) {
        location.href = "/login";
        return;
      }
      const data = await response.json();
      if (!response.ok || !data.status) throw new Error(data.error || "Статус недоступен");
      value.textContent = data.status;
      const state = data.status.replace(/^#/, "");
      root.title = data.cmts ? "CMTS: " + data.cmts : "";
      root.dataset.state = state === "offline" || state === "shutdown" ? "offline"
        : state.startsWith("p-") || state.includes("(d)")
          || !state.includes("online") ? "warning" : "online";
    } catch (error) {
      value.textContent = "n/a";
      root.dataset.state = "unknown";
      root.title = error.name === "AbortError" ? "Таймаут опроса CMTS" : error.message;
    } finally {
      clearTimeout(timeout);
      inFlight = false;
    }
  }
  refresh();
  const timer = setInterval(refresh, 5000);
  window.addEventListener("pagehide", () => clearInterval(timer), {once: true});
})();
