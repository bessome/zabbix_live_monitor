(() => {
  const root = document.getElementById("onu-monitor");
  if (!root) return;
  const query = document.getElementById("onu-query");
  const olt = document.getElementById("onu-olt");
  const rows = document.getElementById("onu-rows");
  const status = document.getElementById("onu-status");
  const dialog = document.getElementById("onu-history-dialog");
  const title = document.getElementById("onu-history-title");
  const close = document.getElementById("onu-history-close");
  const canvas = document.getElementById("onu-history-canvas");
  const historyStatus = document.getElementById("onu-history-status");
  const stats = document.getElementById("onu-history-stats");
  const periods = [...document.querySelectorAll("#onu-history-periods button")];
  const timezone = document.documentElement.dataset.timezone;
  const updatedFormat = new Intl.DateTimeFormat("ru-RU", {
    timeZone: timezone, day: "2-digit", month: "2-digit",
    hour: "2-digit", minute: "2-digit"
  });
  let searchController = null;
  let searchTimer = null;
  let historyController = null;
  let historyTimer = null;
  let historyUrl = "";
  let period = "1h";
  let latest = null;
  const intervals = {"1h": 30000, "12h": 60000, "24h": 120000,
    "2d": 120000, "14d": 300000};

  async function request(url, signal) {
    const response = await fetch(url, {credentials: "same-origin", cache: "no-store", signal});
    if (response.status === 401) {
      location.href = "/login";
      throw new Error("Требуется вход в приложение.");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Zabbix недоступен.");
    return data;
  }

  function cell(tr, label, value) {
    const td = document.createElement("td");
    td.dataset.label = label;
    td.textContent = value;
    tr.appendChild(td);
    return td;
  }

  function showItems(data) {
    rows.replaceChildren();
    for (const item of data.items) {
      const tr = document.createElement("tr");
      const name = cell(tr, "Item", item.name);
      name.className = "onu-item-name";
      const key = document.createElement("small");
      key.className = "onu-item-key";
      key.textContent = item.key;
      name.appendChild(key);
      cell(tr, "OLT", item.olt);
      const value = cell(tr, "Значение", item.value === null ? "—" :
        String(item.value) + (item.units ? " " + item.units : ""));
      value.className = "onu-value";
      cell(tr, "Обновлено", item.updated_at ?
        updatedFormat.format(new Date(item.updated_at * 1000)) : "—");
      const actions = cell(tr, "", "");
      if (item.numeric) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "onu-graph-button";
        button.textContent = "График";
        button.setAttribute("aria-label", "История " + item.name);
        button.addEventListener("click", () => openHistory(item));
        actions.appendChild(button);
      }
      rows.appendChild(tr);
    }
    status.textContent = data.hint || (data.items.length
      ? "Найдено " + data.items.length + (data.has_more ? "+ items. Уточните поиск." : " items.")
      : "Items не найдены.");
  }

  async function search() {
    if (searchController) searchController.abort();
    const active = new AbortController();
    searchController = active;
    status.textContent = "Поиск items в Zabbix…";
    const params = new URLSearchParams({q: query.value.trim(), host_id: olt.value});
    try {
      const data = await request("/api/onu-ont/items?" + params, active.signal);
      if (searchController === active) showItems(data);
    } catch (error) {
      if (error.name !== "AbortError" && searchController === active) {
        rows.replaceChildren();
        status.textContent = error.message;
      }
    } finally {
      if (searchController === active) searchController = null;
    }
  }

  query.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(search, 300);
  });
  olt.addEventListener("change", search);

  function clearChart() {
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  }

  function draw(data) {
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    const dark = document.documentElement.dataset.theme === "dark";
    const colors = dark
      ? {grid: "#40566a", text: "#adbdcc", line: "#69b8f1"}
      : {grid: "#dce6ef", text: "#607185", line: "#176cad"};
    const left = width < 420 ? 46 : 58;
    const top = 19;
    const plotWidth = Math.max(1, width - left - 12);
    const plotHeight = Math.max(1, height - top - 32);
    const values = data.points.map(point => point.value);
    const low = values.length ? Math.min(...values) : 0;
    const high = values.length ? Math.max(...values) : 1;
    const padding = Math.max((high - low) * .12, Math.abs(high) * .02, .1);
    const minY = low - padding;
    const maxY = high + padding;
    const x = time => left + (time - data.from) / (data.to - data.from) * plotWidth;
    const y = value => top + (maxY - value) / (maxY - minY) * plotHeight;
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillStyle = colors.text;
    ctx.fillText(data.units, left, 12);
    for (let i = 0; i <= 4; i++) {
      const yy = top + i / 4 * plotHeight;
      ctx.strokeStyle = colors.grid;
      ctx.beginPath();
      ctx.moveTo(left, yy);
      ctx.lineTo(left + plotWidth, yy);
      ctx.stroke();
      ctx.fillStyle = colors.text;
      ctx.textAlign = "right";
      ctx.fillText((maxY - i / 4 * (maxY - minY)).toFixed(1), left - 5, yy + 4);
    }
    const timeFormat = new Intl.DateTimeFormat("ru-RU", {
      timeZone: timezone, hour: "2-digit", minute: "2-digit",
      ...(period === "1h" ? {} : {day: "2-digit", month: "2-digit"})
    });
    const ticks = width < 500 ? 2 : 4;
    for (let i = 0; i <= ticks; i++) {
      const xx = left + i / ticks * plotWidth;
      ctx.textAlign = i === 0 ? "left" : i === ticks ? "right" : "center";
      ctx.fillText(timeFormat.format(new Date((data.from + i / ticks *
        (data.to - data.from)) * 1000)), xx, height - 8);
    }
    if (!data.points.length) return;
    ctx.strokeStyle = colors.line;
    ctx.lineWidth = 2;
    ctx.beginPath();
    const gap = Math.max(300, 3 * (data.to - data.from) / data.points.length);
    data.points.forEach((point, index) => {
      if (!index || point.time - data.points[index - 1].time > gap)
        ctx.moveTo(x(point.time), y(point.value));
      else ctx.lineTo(x(point.time), y(point.value));
    });
    ctx.stroke();
  }

  function showHistory(data) {
    latest = data;
    title.textContent = data.label;
    historyStatus.textContent = data.points.length
      ? (data.aggregation === "hourly_average" ? "Почасовое среднее Zabbix" : "")
      : "За выбранный период данных нет.";
    stats.replaceChildren();
    if (data.points.length) {
      const values = data.points.map(point => point.value);
      const unit = data.units ? " " + data.units : "";
      for (const [label, value] of [["Последнее", values.at(-1)],
        ["Мин", Math.min(...values)], ["Макс", Math.max(...values)]]) {
        const span = document.createElement("span");
        span.textContent = label + ": " + Number(value.toFixed(2)) + unit;
        stats.appendChild(span);
      }
    }
    canvas.setAttribute("aria-label", "История " + data.label + " за " + data.period);
    draw(data);
  }

  async function refreshHistory() {
    if (!dialog.open || !historyUrl || historyController) return;
    const url = historyUrl;
    const selectedPeriod = period;
    const active = new AbortController();
    historyController = active;
    try {
      const data = await request(url + "?period=" + encodeURIComponent(selectedPeriod),
        active.signal);
      if (dialog.open && historyUrl === url && period === selectedPeriod) showHistory(data);
    } catch (error) {
      if (error.name !== "AbortError" && dialog.open && historyUrl === url
          && period === selectedPeriod) historyStatus.textContent = error.message;
    } finally {
      if (historyController === active) historyController = null;
    }
  }

  function resetHistory() {
    if (historyController) {
      historyController.abort();
      historyController = null;
    }
    latest = null;
    stats.replaceChildren();
    clearChart();
    historyStatus.textContent = "Загрузка истории Zabbix…";
    refreshHistory();
    clearInterval(historyTimer);
    historyTimer = setInterval(refreshHistory, intervals[period]);
  }

  function openHistory(item) {
    historyUrl = "/api/onu-ont/olts/" + encodeURIComponent(item.host_id)
      + "/items/" + encodeURIComponent(item.id) + "/history";
    title.textContent = item.name;
    dialog.showModal();
    resetHistory();
  }

  periods.forEach(button => button.addEventListener("click", () => {
    if (period === button.dataset.period) return;
    period = button.dataset.period;
    periods.forEach(option => option.setAttribute("aria-pressed", String(option === button)));
    resetHistory();
  }));
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    clearInterval(historyTimer);
    historyTimer = null;
    if (historyController) historyController.abort();
    historyController = null;
    historyUrl = "";
  });
  window.addEventListener("resize", () => { if (dialog.open && latest) draw(latest); });

  request("/api/onu-ont/olts").then(data => {
    for (const host of data.olts) {
      const option = document.createElement("option");
      option.value = host.id;
      option.textContent = host.name;
      olt.appendChild(option);
    }
    if (data.olts.length) search();
    else status.textContent = "В группе Zabbix 100 нет доступных OLT.";
  }).catch(error => { status.textContent = error.message; });
  setInterval(() => { if (query.value.trim() || olt.value) search(); }, 60000);
})();
