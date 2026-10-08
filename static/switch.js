(() => {
  const root = document.getElementById("switch-monitor");
  if (!root) return;
  const list = document.getElementById("switch-ports");
  const state = document.getElementById("switch-state");
  const dialog = document.getElementById("switch-cable-dialog");
  const dialogTitle = document.getElementById("switch-cable-title");
  const dialogPort = document.getElementById("switch-cable-port");
  const dialogStatus = document.getElementById("switch-cable-status");
  const trafficDown = document.getElementById("switch-traffic-down");
  const trafficUp = document.getElementById("switch-traffic-up");
  const results = document.getElementById("switch-cable-results");
  const pairs = document.getElementById("switch-cable-pairs");
  const start = document.getElementById("switch-cable-start");
  const csrf = document.getElementById("switch-cable-csrf");
  const url = "/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId) + "/switch-ports";
  const displayTime = new Intl.DateTimeFormat("en-GB", {
    timeZone: document.documentElement.dataset.timezone,
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
  });
  let inFlight = false;
  let selectedPort = null;
  let testInFlight = false;
  let cableCountdownTimer = null;
  let trafficTimer = null;
  let trafficController = null;

  function stopCableCountdown() {
    clearInterval(cableCountdownTimer);
    cableCountdownTimer = null;
  }

  function formatTraffic(bps) {
    if (bps == null || !Number.isFinite(bps)) return "n/a";
    const units = bps >= 1e9 ? [1e9, "Гбит/с"]
      : bps >= 1e6 ? [1e6, "Мбит/с"]
      : bps >= 1e3 ? [1e3, "Кбит/с"] : [1, "бит/с"];
    return new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 1})
      .format(bps / units[0]) + " " + units[1];
  }

  async function refreshTraffic() {
    if (!dialog.open || !selectedPort || trafficController) return;
    const port = selectedPort;
    const controller = new AbortController();
    const started = performance.now();
    trafficController = controller;
    try {
      const response = await fetch(url + "/" + encodeURIComponent(port.index)
        + "/traffic", {credentials: "same-origin", cache: "no-store",
          signal: controller.signal});
      if (response.status === 401) {
        location.href = "/login";
        return;
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "SNMP недоступен");
      if (dialog.open && selectedPort === port) {
        trafficDown.textContent = formatTraffic(data.down_bps);
        trafficUp.textContent = formatTraffic(data.up_bps);
        trafficDown.parentElement.title = data.error || "Из коммутатора в устройство";
        trafficUp.parentElement.title = data.error || "Из устройства в коммутатор";
      }
    } catch (error) {
      if (error.name !== "AbortError" && dialog.open && selectedPort === port) {
        trafficDown.textContent = "n/a";
        trafficUp.textContent = "n/a";
        trafficDown.parentElement.title = error.message;
        trafficUp.parentElement.title = error.message;
      }
    } finally {
      if (trafficController === controller) trafficController = null;
      if (dialog.open && selectedPort === port) {
        trafficTimer = setTimeout(refreshTraffic,
          Math.max(0, 5000 - (performance.now() - started)));
      }
    }
  }

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
      tile.title = port.name + " · " + status + " · нагрузка и тест кабеля";
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
    dialogTitle.textContent = "Порт · " + selectedPort.name;
    dialogPort.textContent = "Порт " + selectedPort.name;
    dialogStatus.textContent = "";
    pairs.replaceChildren();
    results.hidden = true;
    trafficDown.textContent = "n/a";
    trafficUp.textContent = "n/a";
    dialog.showModal();
    clearTimeout(trafficTimer);
    refreshTraffic();
  });

  document.getElementById("switch-cable-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    stopCableCountdown();
    clearTimeout(trafficTimer);
    trafficTimer = null;
    if (trafficController) {
      trafficController.abort();
      trafficController = null;
    }
    selectedPort = null;
  });
  if (start) start.addEventListener("click", async () => {
    if (!selectedPort || testInFlight) return;
    const port = selectedPort;
    testInFlight = true;
    start.disabled = true;
    results.hidden = true;
    pairs.replaceChildren();
    const countdownUntil = performance.now() + 5000;
    const updateCountdown = () => {
      const seconds = Math.max(0, Math.ceil((countdownUntil - performance.now()) / 1000));
      dialogStatus.textContent = seconds
        ? "Тест идёт… ~" + seconds + " с"
        : "Тест идёт… ожидаем результат";
    };
    updateCountdown();
    stopCableCountdown();
    cableCountdownTimer = setInterval(updateCountdown, 250);
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
      stopCableCountdown();
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
        ? "Обновлено " + displayTime.format(new Date(data.updated_at * 1000))
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
