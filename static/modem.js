(() => {
  const root = document.getElementById("modem-monitor");
  if (!root) return;
  const list = document.getElementById("modem-channels");
  const state = document.getElementById("modem-state");
  const restartButton = document.getElementById("modem-restarts");
  const restartValue = document.getElementById("modem-restarts-value");
  const dailyRestarts = document.getElementById("modem-restarts-daily");
  const direct = root.dataset.directSnmp === "true";
  const url = (root.dataset.apiBase || ("/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId))) + "/modem-channels";
  const restartUrl = url.replace(/\/modem-channels$/, "/modem-restarts");
  let signature = "";
  let inFlight = false;
  let restartInFlight = false;
  let previousRestartValue;
  const previousValues = new Map();
  const previousErrors = new Map();

  function render(items) {
    const nextSignature = items.map(item => item.id + ":" + item.label
      + ("error_rate" in item ? ":errors" : "")).join("|");
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
        const row = document.createElement(direct ? "div" : "button");
        if (!direct) row.type = "button";
        row.className = "modem-row";
        if ("error_rate" in item) row.classList.add("modem-row-with-errors");
        row.dataset.itemId = item.id;
        row.title = direct ? "Текущие данные SNMP" : "Открыть график Zabbix за последний час";
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
        if ("error_rate" in item) {
          const errors = document.createElement("span");
          errors.className = "modem-error-rate";
          errors.title = "Ошибок в секунду";
          value.append(errors);
        }
        row.append(label, value);
        list.appendChild(row);
      }
      signature = nextSignature;
    }
    const activeIds = new Set(items.map(item => item.id));
    for (const id of previousValues.keys()) {
      if (!activeIds.has(id)) previousValues.delete(id);
    }
    for (const id of previousErrors.keys()) {
      if (!activeIds.has(id)) previousErrors.delete(id);
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
      number.textContent = item.value == null ? "n/a" : item.value;
      units.textContent = item.value == null || !item.units ? "" : " " + item.units;
      number.classList.toggle("modem-value-changed", changed);
      previousValues.set(item.id, item.value);
      const errors = value.querySelector(".modem-error-rate");
      if (errors) {
        const errorChanged = previousErrors.has(item.id)
          && previousErrors.get(item.id) !== item.error_rate && item.error_rate != null;
        errors.textContent = " (" + (item.error_rate == null ? "n/a" : item.error_rate + "/с") + ")";
        errors.classList.toggle("modem-value-changed", errorChanged);
        previousErrors.set(item.id, item.error_rate);
      }
      const numericValue = item.value == null ? NaN : Number(item.value);
      const lowSnr = item.label.endsWith(" SNR") && numericValue < 30;
      const highUs = item.label.startsWith("US") && numericValue > 52;
      value.classList.toggle("modem-value-alert-blink", lowSnr);
      value.classList.toggle("modem-value-alert", highUs);
      value.title = lowSnr ? "SNR ниже 30" : highUs ? "US Level выше 52" : "";
    }
  }

  function markUnavailable() {
    for (const row of list.querySelectorAll(".modem-row")) {
      row.querySelector(".modem-number").textContent = "n/a";
      row.querySelector(".modem-number").classList.remove("modem-value-changed");
      row.querySelector(".modem-units").textContent = "";
      const errors = row.querySelector(".modem-error-rate");
      if (errors) {
        errors.textContent = " (n/a)";
        errors.classList.remove("modem-value-changed");
        previousErrors.set(row.dataset.itemId, null);
      }
      const value = row.querySelector("strong");
      value.classList.remove("modem-value-alert-blink", "modem-value-alert");
      value.title = "";
      previousValues.set(row.dataset.itemId, null);
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
      if (!response.ok) {
        if (data.items?.length) render(data.items);
        throw new Error(data.error || "SNMP недоступен");
      }
      render(data.items);
      state.textContent = data.items.length
        ? ""
        : "Показатели каналов для этого модема не найдены.";
    } catch (error) {
      markUnavailable();
      state.textContent = error.message;
    } finally {
      inFlight = false;
    }
  }

  async function refreshRestarts() {
    if (restartInFlight) return;
    restartInFlight = true;
    try {
      const response = await fetch(restartUrl, {credentials: "same-origin", cache: "no-store"});
      if (response.status === 401) {
        location.href = "/login";
        return;
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Zabbix недоступен");
      restartButton.hidden = !data.item;
      dailyRestarts.hidden = data.count_24h == null;
      if (data.count_24h != null) {
        dailyRestarts.textContent = "\u00a0(" + data.count_24h + ")";
        dailyRestarts.setAttribute("aria-label", "Рестартов за последние 24 часа: " + data.count_24h);
      }
      if (!data.item) return;
      restartButton.dataset.itemId = data.item.id;
      const changed = previousRestartValue !== undefined && data.item.value != null
        && previousRestartValue !== data.item.value;
      restartValue.textContent = data.item.value == null ? "n/a" : String(data.item.value);
      restartValue.classList.toggle("modem-value-changed", changed);
      previousRestartValue = data.item.value;
      restartButton.title = "Открыть историю рестартов Zabbix";
    } catch (error) {
      dailyRestarts.hidden = true;
      if (!restartButton.hidden) {
        restartValue.textContent = "n/a";
        restartValue.classList.remove("modem-value-changed");
        restartButton.title = error.message;
      }
    } finally {
      restartInFlight = false;
    }
  }

  refresh();
  setInterval(refresh, 5000);
  if (!direct) {
    refreshRestarts();
    setInterval(refreshRestarts, 15000);
  }
})();
