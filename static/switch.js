(() => {
  const root = document.getElementById("switch-monitor");
  if (!root) return;
  const list = document.getElementById("switch-ports");
  const state = document.getElementById("switch-state");
  const url = "/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId) + "/switch-ports";
  let inFlight = false;

  function render(ports) {
    const fragment = document.createDocumentFragment();
    for (const port of ports) {
      const tile = document.createElement("div");
      const label = document.createElement("strong");
      const status = port.state === "down" ? "нет линка"
        : port.speed_mbps == null ? "скорость неизвестна"
        : port.speed_mbps >= 1000 ? (port.speed_mbps / 1000) + " Гбит/с"
        : port.speed_mbps + " Мбит/с";
      tile.className = "switch-port " + port.state;
      tile.title = port.name + " · " + status;
      tile.setAttribute("aria-label", tile.title);
      label.textContent = port.label;
      tile.append(label);
      fragment.appendChild(tile);
    }
    list.replaceChildren(fragment);
  }

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
        ? "Обновлено " + new Date(data.updated_at * 1000).toLocaleTimeString()
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
