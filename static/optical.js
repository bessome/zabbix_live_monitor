(() => {
  const root = document.getElementById("optical-monitor");
  if (!root) return;
  const list = document.getElementById("optical-channels");
  const state = document.getElementById("optical-state");
  const url = "/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId)
    + ({EDFA: "/edfa-levels", UPSs: "/ups-levels"}[root.dataset.category] || "/optical-power");
  const ups = root.dataset.category === "UPSs";
  const previousValues = new Map();
  let signature = "";
  let inFlight = false;

  function render(items) {
    if (ups) {
      document.dispatchEvent(new CustomEvent("ups-load-item", {detail: items.find(item => item.label === "Output Load L1") || null}));
      items = ["Output Load L1", "Battery Capacity", "Autonomy"].map((label, index) =>
        items.find(item => item.label === label) || {id: "missing:" + index, label, value: null});
    }
    const nextSignature = items.map(item => item.id).join("|");
    if (nextSignature !== signature) {
      list.replaceChildren();
      items.forEach(item => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "optical-row";
        row.dataset.itemId = item.id;
        row.disabled = item.id.startsWith("missing:");
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
      const valid = item.value != null && item.value !== "" && Number.isFinite(Number(item.value));
      number.textContent = valid ? String(Number(Number(item.value).toFixed(2))) : "n/a";
      units.textContent = !valid || !item.units ? "" : " " + item.units;
      number.classList.toggle("modem-value-changed", changed);
      previousValues.set(item.id, item.value);
      row.querySelector("strong").classList.toggle("optical-value-alert",
        valid && (["EDFA", "UPSs"].includes(root.dataset.category) ? item.state === "critical"
          : Number(item.raw_value ?? item.value) < -3.4));
      row.querySelector("strong").classList.toggle("optical-value-warning",
        valid && ["EDFA", "UPSs"].includes(root.dataset.category) && item.state === "warning");
    }
  }

  function markUnavailable() {
    for (const row of list.querySelectorAll(".optical-row")) {
      row.querySelector(".modem-number").textContent = "n/a";
      row.querySelector(".modem-number").classList.remove("modem-value-changed");
      row.querySelector(".modem-units").textContent = "";
      row.querySelector("strong").classList.remove("optical-value-alert");
      row.querySelector("strong").classList.remove("optical-value-warning");
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
        throw new Error(data.error || "Показатели недоступны");
      }
      render(data.items);
      state.textContent = data.items.length
        ? "" : "Показатели для этого устройства не найдены в Zabbix.";
    } catch (error) {
      if (ups) render([]);
      markUnavailable();
      state.textContent = error.message;
    } finally {
      inFlight = false;
    }
  }

  refresh();
  setInterval(refresh, 5000);
})();
