(() => {
  const root = document.getElementById("switch-monitor");
  if (!root) return;
  const list = document.getElementById("switch-ports");
  const state = document.getElementById("switch-state");
  const dialog = document.getElementById("switch-cable-dialog");
  const dialogTitle = document.getElementById("switch-cable-title");
  const dialogPort = document.getElementById("switch-cable-port");
  const dialogStatus = document.getElementById("switch-cable-status");
  const results = document.getElementById("switch-cable-results");
  const pairs = document.getElementById("switch-cable-pairs");
  const start = document.getElementById("switch-cable-start");
  const csrf = document.getElementById("switch-cable-csrf");
  const url = "/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId) + "/switch-ports";
  let inFlight = false;
  let selectedPort = null;
  let testInFlight = false;

  function render(ports) {
    const fragment = document.createDocumentFragment();
    for (const port of ports) {
      const tile = document.createElement("button");
      tile.type = "button";
      const label = document.createElement("strong");
      const status = port.state === "down" ? "нет линка"
        : port.speed_mbps == null ? "скорость неизвестна"
        : port.speed_mbps >= 1000 ? (port.speed_mbps / 1000) + " Гбит/с"
        : port.speed_mbps + " Мбит/с";
      tile.className = "switch-port " + port.state;
      tile.title = port.name + " · " + status + " · открыть тест кабеля";
      tile.setAttribute("aria-label", tile.title);
      tile.dataset.index = String(port.index);
      tile.dataset.name = port.name;
      label.textContent = port.label;
      tile.append(label);
      fragment.appendChild(tile);
    }
    list.replaceChildren(fragment);
  }

  list.addEventListener("click", event => {
    const tile = event.target.closest(".switch-port");
    if (!tile || !list.contains(tile) || testInFlight) return;
    selectedPort = {index: tile.dataset.index, name: tile.dataset.name};
    dialogTitle.textContent = "Тест кабеля · " + selectedPort.name;
    dialogPort.textContent = "Порт " + selectedPort.name;
    dialogStatus.textContent = "";
    pairs.replaceChildren();
    results.hidden = true;
    dialog.showModal();
  });

  document.getElementById("switch-cable-close").addEventListener("click", () => dialog.close());
  if (start) start.addEventListener("click", async () => {
    if (!selectedPort || testInFlight) return;
    const port = selectedPort;
    testInFlight = true;
    start.disabled = true;
    results.hidden = true;
    pairs.replaceChildren();
    dialogStatus.textContent = "Тест идёт… Результат примерно через 5 секунд.";
    try {
      const body = new FormData();
      body.append("csrf_token", csrf.value);
      const response = await fetch(url + "/" + encodeURIComponent(port.index)
        + "/cable-test", {method: "POST", credentials: "same-origin", body});
      if (response.status === 401) {
        location.href = "/login";
        return;
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || data.detail || "Тест не выполнен");
      const fragment = document.createDocumentFragment();
      for (const pair of data.pairs) {
        const row = document.createElement("tr");
        for (const value of [pair.pair, pair.status, pair.length, pair.error]) {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.appendChild(cell);
        }
        fragment.appendChild(row);
      }
      pairs.replaceChildren(fragment);
      results.hidden = false;
      dialogStatus.textContent = "Результат для порта " + data.port;
    } catch (error) {
      dialogStatus.textContent = error.message;
    } finally {
      testInFlight = false;
      start.disabled = false;
    }
  });

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    try {
      const response = await fetch(url, {credentials: "same-origin", cache: "no-store"});
      if (response.status === 401) {
        location.href = "/login";
        return;
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "SNMP недоступен");
      render(data.ports);
      state.textContent = data.ports.length
        ? "Обновлено " + new Date(data.updated_at * 1000).toLocaleTimeString("en-GB", {hour12: false})
        : "Физические Ethernet-порты не найдены";
    } catch (error) {
      list.replaceChildren();
      state.textContent = error.message;
    } finally {
      inFlight = false;
    }
  }

  refresh();
  setInterval(refresh, 5000);
})();
