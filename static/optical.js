(() => {
  const root = document.getElementById("optical-monitor");
  if (!root) return;
  const list = document.getElementById("optical-channels");
  const state = document.getElementById("optical-state");
  const url = "/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId) + "/optical-power";
  const previousValues = new Map();
  let signature = "";
  let inFlight = false;

  function render(items) {
    const nextSignature = items.map(item => item.id).join("|");
    if (nextSignature !== signature) {
      list.replaceChildren();
      items.forEach(item => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "optical-row";
        row.dataset.itemId = item.id;
        row.title = "Открыть график Zabbix";
        const label = document.createElement("span");
        label.textContent = item.label;
        const value = document.createElement("strong");
        const number = document.createElement("span");
        number.className = "modem-number";
        const units = document.createElement("span");
        units.className = "modem-units";
        value.append(number, units);
        row.append(label, value);
        list.appendChild(row);
      });
      signature = nextSignature;
    }
    const activeIds = new Set(items.map(item => item.id));
    for (const id of previousValues.keys()) {
      if (!activeIds.has(id)) previousValues.delete(id);
    }
    for (const item of items) {
      const row = [...list.querySelectorAll(".optical-row")]
        .find(element => element.dataset.itemId === item.id);
      if (!row) continue;
      const number = row.querySelector(".modem-number");
      const units = row.querySelector(".modem-units");
      const changed = previousValues.has(item.id)
        && previousValues.get(item.id) !== item.value && item.value != null;
      number.textContent = item.value == null ? "—" : item.value;
      units.textContent = item.value == null || !item.units ? "" : " " + item.units;
      number.classList.toggle("modem-value-changed", changed);
      previousValues.set(item.id, item.value);
      row.querySelector("strong").classList.toggle("optical-value-alert",
        item.value != null && Number(item.value) < -3.3);
    }
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
      render(data.items);
      state.textContent = data.items.length
        ? "" : "Показатель Optical input power для этого устройства не найден.";
    } catch (error) {
      state.textContent = error.message;
    } finally {
      inFlight = false;
    }
  }

  refresh();
  setInterval(refresh, 5000);
})();
