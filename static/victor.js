(() => {
  const root = document.getElementById("modem-monitor");
  if (!root) return;
  const open = document.getElementById("victor-open");
  const dialog = document.getElementById("victor-dialog");
  const close = document.getElementById("victor-close");
  const canvas = document.getElementById("victor-canvas");
  const status = document.getElementById("victor-status");
  const legend = document.getElementById("victor-legend");
  const buttons = [...document.querySelectorAll("#victor-periods button")];
  const url = "/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId) + "/modem-overview/history";
  const timezone = document.documentElement.dataset.timezone;
  const timeFormat = new Intl.DateTimeFormat("ru-RU", {
    timeZone: timezone, day: "2-digit", month: "2-digit",
    hour: "2-digit", minute: "2-digit"
  });
  const hourFormat = new Intl.DateTimeFormat("ru-RU", {
    timeZone: timezone, hour: "2-digit", minute: "2-digit"
  });
  const rows = [
    {key: "ds_snr", name: "DS S/N ratio", color: "#00ba12", fill: true},
    {key: "us", name: "US Level", color: "#1234e8"},
    {key: "ds_level", name: "DS Level", color: "#e98900"}
  ];
  const intervals = {"1h": 30000, "12h": 60000, "24h": 120000,
    "2d": 120000, "14d": 300000};
  let period = "1h";
  let latest = null;
  let controller = null;
  let timer = null;

  function format(value) {
    return Number(value.toFixed(2)).toFixed(2);
  }

  function clearChart() {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  function segments(points, from, to) {
    if (!points.length) return [];
    const gap = Math.max(300, 3 * (to - from) / points.length);
    const parts = [];
    let part = [];
    for (const point of points) {
      if (part.length && point.time - part.at(-1).time > gap) {
        parts.push(part);
        part = [];
      }
      part.push(point);
    }
    if (part.length) parts.push(part);
    return parts;
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
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, width, height);

    const left = width < 460 ? 43 : 54;
    const right = 12;
    const top = 16;
    const bottom = 34;
    const plotWidth = Math.max(1, width - left - right);
    const plotHeight = Math.max(1, height - top - bottom);
    const byKey = Object.fromEntries(data.series.map(series => [series.key, series]));
    const values = rows.flatMap(row => (byKey[row.key]?.points || []).map(point => point.value));
    const low = Math.floor((Math.min(0, ...values) - 5) / 10) * 10;
    const high = Math.ceil((Math.max(0, ...values) + 5) / 10) * 10;
    const x = time => left + (time - data.from) / (data.to - data.from) * plotWidth;
    const y = value => top + (high - value) / (high - low) * plotHeight;

    for (let index = 0; index <= 24; index++) {
      const xx = left + index / 24 * plotWidth;
      ctx.strokeStyle = index % 6 === 0 ? "#e1baba" : "#f2e7e7";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xx + .5, top);
      ctx.lineTo(xx + .5, top + plotHeight);
      ctx.stroke();
    }
    ctx.font = "11px Consolas, monospace";
    for (let value = low; value <= high; value += 5) {
      const yy = y(value);
      ctx.strokeStyle = value % 10 === 0 ? "#e1baba" : "#f2e7e7";
      ctx.beginPath();
      ctx.moveTo(left, yy + .5);
      ctx.lineTo(left + plotWidth, yy + .5);
      ctx.stroke();
      if (value % 10 === 0) {
        ctx.fillStyle = "#424242";
        ctx.textAlign = "right";
        ctx.fillText(String(value), left - 7, yy + 4);
      }
    }

    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, plotWidth, plotHeight);
    ctx.clip();
    for (const row of rows) {
      const points = byKey[row.key]?.points || [];
      for (const part of segments(points, data.from, data.to)) {
        if (row.fill) {
          ctx.beginPath();
          ctx.moveTo(x(part[0].time), y(0));
          for (const point of part) ctx.lineTo(x(point.time), y(point.value));
          ctx.lineTo(x(part.at(-1).time), y(0));
          ctx.closePath();
          ctx.fillStyle = "rgba(0, 194, 18, .88)";
          ctx.fill();
        }
        ctx.beginPath();
        part.forEach((point, index) => {
          if (index === 0) ctx.moveTo(x(point.time), y(point.value));
          else ctx.lineTo(x(point.time), y(point.value));
        });
        ctx.strokeStyle = row.fill ? "#008509" : row.color;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }
    ctx.restore();

    const tickCount = width < 500 ? 2 : 4;
    ctx.fillStyle = "#333";
    for (let index = 0; index <= tickCount; index++) {
      const xx = left + index / tickCount * plotWidth;
      ctx.textAlign = index === 0 ? "left" : index === tickCount ? "right" : "center";
      const time = new Date((data.from + index / tickCount * (data.to - data.from)) * 1000);
      ctx.fillText((period === "1h" ? hourFormat : timeFormat).format(time), xx, height - 9);
    }
    if (!values.length) {
      ctx.fillStyle = "#555";
      ctx.textAlign = "center";
      ctx.fillText("Нет данных за выбранный период", left + plotWidth / 2,
        top + plotHeight / 2);
    }
  }

  function show(data) {
    latest = data;
    const byKey = Object.fromEntries(data.series.map(series => [series.key, series]));
    legend.replaceChildren();
    for (const row of rows) {
      const series = byKey[row.key];
      const points = series?.points || [];
      const entry = document.createElement("div");
      entry.className = "victor-legend-row";
      const swatch = document.createElement("span");
      swatch.className = "victor-legend-swatch";
      swatch.style.backgroundColor = row.color;
      const name = document.createElement("span");
      name.className = "victor-legend-name";
      name.textContent = (series?.label || row.name) + (series?.units ? " (" + series.units + ")" : "");
      const statistics = document.createElement("span");
      statistics.className = "victor-legend-values";
      const values = points.map(point => point.value);
      for (const [label, value] of [["last", points.at(-1)?.value],
        ["min", values.length ? Math.min(...values) : undefined],
        ["max", values.length ? Math.max(...values) : undefined]]) {
        const cell = document.createElement("span");
        cell.textContent = label + ": " + (value === undefined ? "—" : format(value));
        statistics.appendChild(cell);
      }
      entry.append(swatch, name, statistics);
      legend.appendChild(entry);
    }
    status.textContent = data.series.some(series => series.aggregation === "hourly_average")
      ? "14d — почасовое среднее Zabbix" : "";
    canvas.setAttribute("aria-label", "График DS SNR, US Level и DS Level за " + data.period);
    draw(data);
  }

  async function refresh() {
    if (!dialog.open || controller) return;
    const requestedPeriod = period;
    const active = new AbortController();
    controller = active;
    try {
      const response = await fetch(url + "?period=" + encodeURIComponent(requestedPeriod),
        {credentials: "same-origin", cache: "no-store", signal: active.signal});
      if (response.status === 401) {
        location.href = "/login";
        return;
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "История Zabbix недоступна");
      if (dialog.open && period === requestedPeriod) show(data);
    } catch (error) {
      if (error.name !== "AbortError" && dialog.open && period === requestedPeriod) {
        status.textContent = error.message;
      }
    } finally {
      if (controller === active) controller = null;
    }
  }

  function scheduleRefresh() {
    clearInterval(timer);
    timer = setInterval(refresh, intervals[period]);
  }

  open.addEventListener("click", () => {
    latest = null;
    legend.replaceChildren();
    clearChart();
    status.textContent = "Загрузка истории Zabbix…";
    dialog.showModal();
    refresh();
    scheduleRefresh();
  });
  for (const button of buttons) {
    button.addEventListener("click", () => {
      if (period === button.dataset.period) return;
      period = button.dataset.period;
      for (const option of buttons) option.setAttribute("aria-pressed", String(option === button));
      if (controller) {
        controller.abort();
        controller = null;
      }
      latest = null;
      legend.replaceChildren();
      clearChart();
      status.textContent = "Загрузка истории Zabbix…";
      refresh();
      scheduleRefresh();
    });
  }
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    clearInterval(timer);
    timer = null;
    if (controller) {
      controller.abort();
      controller = null;
    }
  });
  window.addEventListener("resize", () => { if (dialog.open && latest) draw(latest); });
})();
