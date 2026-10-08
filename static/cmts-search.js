(() => {
  const open = document.getElementById("cmts-search-open");
  if (!open) return;
  const dialog = document.getElementById("cmts-search-dialog");
  const detail = document.getElementById("cmts-detail-dialog");
  const frame = document.getElementById("cmts-detail-frame");
  const form = document.getElementById("cmts-search-form");
  const query = document.getElementById("cmts-mac-query");
  const selectedCmts = document.getElementById("cmts-select");
  const results = document.getElementById("cmts-search-results");
  const status = document.getElementById("cmts-search-status");
  const close = document.getElementById("cmts-search-close");
  const detailClose = document.getElementById("cmts-detail-close");
  let controller;

  open.addEventListener("click", () => dialog.showModal());
  close.addEventListener("click", () => dialog.close());
  detailClose.addEventListener("click", () => detail.close());
  dialog.addEventListener("close", () => { if (controller) controller.abort(); });
  detail.addEventListener("close", () => {
    frame.src = "about:blank";
    dialog.showModal();
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (controller) controller.abort();
    controller = new AbortController();
    const active = controller;
    results.replaceChildren();
    status.textContent = "Поиск на CMTS…";
    const params = new URLSearchParams({q: query.value.trim()});
    if (selectedCmts.value) params.set("cmts_id", selectedCmts.value);
    try {
      const response = await fetch("/api/cmts/search?" + params, {
        credentials: "same-origin", cache: "no-store", signal: active.signal
      });
      if (response.status === 401) { location.href = "/login"; return; }
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.error || "Поиск недоступен");
      for (const modem of data.results) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "cmts-result";
            const address = document.createElement("strong");
        address.textContent = (modem.ip || "IP неизвестен") + " · " + modem.mac;
        const cmts = document.createElement("small");
        cmts.textContent = modem.cmts_name;
        row.append(address, cmts);
        row.addEventListener("click", () => {
          dialog.close();
          frame.src = "/cmts/" + encodeURIComponent(modem.cmts_id)
            + "/modems/" + encodeURIComponent(modem.mac_compact);
          detail.showModal();
        });
        results.appendChild(row);
      }
      const suffix = data.truncated ? " · Показаны не все совпадения, уточните MAC." : "";
      const errors = data.errors.map(item => item.cmts + ": " + item.error);
      status.textContent = "Найдено: " + data.results.length + suffix
        + (errors.length ? " · Ошибки: " + errors.join("; ") : "");
    } catch (error) {
      if (error.name !== "AbortError") status.textContent = error.message;
    } finally {
      if (controller === active) controller = null;
    }
  });
})();
