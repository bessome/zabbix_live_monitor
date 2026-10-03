(() => {
  const root = document.getElementById("modem-monitor");
  if (!root) return;
  const list = document.getElementById("modem-channels");
  const state = document.getElementById("modem-state");
  const url = "/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId) + "/modem-channels";
  let signature = "";
  let inFlight = false;
  const previousValues = new Map();

  function render(items) {
    const nextSignature = items.map(item => item.id + ":" + item.label).join("|");
    if (nextSignature !== signature) {
      list.replaceChildren();
      let currentGroup = "";
      for (const item of items) {
        const group = item.label.startsWith("US") ? "Upstream" : "Downstream";
        if (group !== currentGroup) {
          const heading = document.createElement("h3");
          heading.className = "modem-group";
          heading.textContent = group;
          list.appendChild(heading);
          currentGroup = group;
        }
        const row = document.createElement("button");
        row.type = "button";
        row.className = "modem-row";
        row.dataset.itemId = item.id;
        row.title = "Открыть график Zabbix за последний час";
        const label = document.createElement("span");
        label.textContent = item.label;
        label.className = item.label.startsWith("US") ? "modem-label-us"
          : item.label.endsWith(" SNR") ? "modem-label-ds-snr" : "modem-label-ds-level";
        const value = document.createElement("strong");
        const number = document.createElement("span");
        number.className = "modem-number";
        const units = document.createElement("span");
        units.className = "modem-units";
        value.append(number, units);
        row.append(label, value);
        list.appendChild(row);
      }
      signature = nextSignature;
    }
    const activeIds = new Set(items.map(item => item.id));
    for (const id of previousValues.keys()) {
      if (!activeIds.has(id)) previousValues.delete(id);
    }
    for (const item of items) {
      const row = [...list.querySelectorAll(".modem-row")]
        .find(element => element.dataset.itemId === item.id);
      if (!row) continue;
      const value = row.querySelector("strong");
      const number = value.querySelector(".modem-number");
      const units = value.querySelector(".modem-units");
      const changed = previousValues.has(item.id)
        && previousValues.get(item.id) !== item.value && item.value != null;
      number.textContent = item.value == null ? "—" : item.value;
      units.textContent = item.value == null || !item.units ? "" : " " + item.units;
      number.classList.toggle("modem-value-changed", changed);
      previousValues.set(item.id, item.value);
      const numericValue = item.value == null ? NaN : Number(item.value);
      const lowSnr = item.label.endsWith(" SNR") && numericValue < 30;
      const highUs = item.label.startsWith("US") && numericValue > 52;
      value.classList.toggle("modem-value-alert-blink", lowSnr);
      value.classList.toggle("modem-value-alert", highUs);
      value.title = lowSnr ? "SNR ниже 30" : highUs ? "US Level выше 52" : "";
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
        ? ""
        : "Показатели каналов для этого модема не найдены.";
    } catch (error) {
      state.textContent = error.message;
    } finally {
      inFlight = false;
    }
  }

  refresh();
  setInterval(refresh, 5000);
})();
