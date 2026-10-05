(() => {
  const root = document.getElementById("ping-monitor");
  if (!root) return;
  const list = document.getElementById("modem-channels");
  const opticalList = document.getElementById("optical-channels");
  const restartButton = document.getElementById("modem-restarts");
  const pingLoss = document.getElementById("ping-loss-history");
  const dialog = document.getElementById("modem-history-dialog");
  const canvas = document.getElementById("modem-history-canvas");
  const status = document.getElementById("modem-history-status");
  const stats = document.getElementById("modem-history-stats");
  const title = document.getElementById("modem-history-title");
  const close = document.getElementById("modem-history-close");
  const periodButtons = [...document.querySelectorAll("#modem-history-periods button")];
  const baseUrl = "/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId);
  const displayTimezone = document.documentElement.dataset.timezone;
  const timeFormat = new Intl.DateTimeFormat("ru-RU", {
    timeZone: displayTimezone, hour: "2-digit", minute: "2-digit"
  });
  const dateTimeFormat = new Intl.DateTimeFormat("ru-RU", {
    timeZone: displayTimezone, day: "2-digit", month: "2-digit",
    hour: "2-digit", minute: "2-digit"
  });
  let historyUrl = null;
  let period = "1h";
  let latest = null;
  let timer = null;
  let controller = null;
  const refreshIntervals = {"1h": 30000, "12h": 60000, "24h": 120000,
    "2d": 120000, "14d": 300000};

  function clearChart() {
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  }

  function scheduleRefresh() {
    clearInterval(timer);
    timer = setInterval(refresh, refreshIntervals[period]);
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
      ? {grid: "#40566a", text: "#adbdcc", line: "#69b8f1", error: "#ffc04d"}
      : {grid: "#dce6ef", text: "#607185", line: "#176cad", error: "#a86400"};
    const left = width < 420 ? 46 : 58;
    const secondary = data.secondary?.points || [];
    const right = data.secondary ? 46 : 12;
    const top = 15;
    const bottom = 28;
    const plotWidth = Math.max(1, width - left - right);
    const plotHeight = Math.max(1, height - top - bottom);
    const values = data.points.map(point => point.value);
    const low = values.length ? Math.min(...values) : 0;
    const high = values.length ? Math.max(...values) : 1;
    const padding = Math.max((high - low) * .12, Math.abs(high) * .02, .1);
    const minY = data.units === "%" ? Math.max(0, low - padding) : low - padding;
    const maxY = data.units === "%" ? Math.min(100, high + padding) : high + padding;
    const errorHigh = secondary.length ? Math.max(...secondary.map(point => point.value)) : 0;
    const errorMax = Math.max(1, errorHigh * 1.1);
    const x = time => left + (time - data.from) / (data.to - data.from) * plotWidth;
    const y = value => top + (maxY - value) / (maxY - minY) * plotHeight;
    const errorY = value => top + (errorMax - value) / errorMax * plotHeight;
    ctx.font = "11px system-ui, sans-serif";
    ctx.lineWidth = 1;
    if (data.secondary) {
      ctx.fillStyle = colors.line;
      ctx.textAlign = "left";
      ctx.fillText(data.units || "SNR", left, 11);
      ctx.fillStyle = colors.error;
      ctx.textAlign = "right";
      ctx.fillText("Ош/с", width - 3, 11);
    }
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
      if (data.secondary) {
        ctx.fillStyle = colors.error;
        ctx.textAlign = "left";
        const tick = errorMax * (1 - i / 4);
        ctx.fillText(tick >= 1000 ? (tick / 1000).toFixed(1) + "k" :
          tick >= 10 ? tick.toFixed(0) : tick.toFixed(1), left + plotWidth + 5, yy + 4);
      }
    }
    const tickCount = data.period === "1h" || width >= 500 ? 4 : 2;
    const labelFormat = data.period === "1h" ? timeFormat : dateTimeFormat;
    for (let i = 0; i <= tickCount; i++) {
      const xx = left + i / tickCount * plotWidth;
      ctx.fillStyle = colors.text;
      ctx.textAlign = i === 0 ? "left" : i === tickCount ? "right" : "center";
      ctx.fillText(labelFormat.format(new Date((data.from + i / tickCount * (data.to - data.from)) * 1000)),
        xx, height - 8);
    }
    function drawSeries(points, scale, color) {
      if (!points.length) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      const gap = Math.max(300, 3 * (data.to - data.from) / points.length);
      points.forEach((point, index) => {
        if (index === 0 || point.time - points[index - 1].time > gap) {
          ctx.moveTo(x(point.time), scale(point.value));
        } else {
          ctx.lineTo(x(point.time), scale(point.value));
        }
      });
      ctx.stroke();
      const last = points[points.length - 1];
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x(last.time), scale(last.value), 3.5, 0, 2 * Math.PI);
      ctx.fill();
    }
    drawSeries(data.points, y, colors.line);
    drawSeries(secondary, errorY, colors.error);
  }

  function show(data) {
    latest = data;
    title.textContent = data.label;
    status.textContent = data.points.length || data.secondary?.points.length
      ? (data.aggregation === "hourly_average" ? "Почасовое среднее Zabbix" : "")
      : "За период " + data.period + " данных в Zabbix нет.";
    canvas.setAttribute("aria-label", "График " + data.label
      + (data.secondary ? " и ошибок в секунду" : "") + " за " + data.period);
    stats.replaceChildren();
    if (data.points.length) {
      const values = data.points.map(point => point.value);
      const last = data.points[data.points.length - 1];
      const unit = data.units ? " " + data.units : "";
      for (const [label, value] of [
        ["Последнее", last.value], ["Мин", Math.min(...values)], ["Макс", Math.max(...values)]
      ]) {
        const element = document.createElement("span");
        element.textContent = label + ": " + Number(value.toFixed(2)) + unit;
        stats.appendChild(element);
      }
    }
    if (data.secondary) {
      const element = document.createElement("span");
      element.className = "history-error-stat";
      const last = data.secondary.points.at(-1);
      element.textContent = "Ошибки/с: " + (last ? Number(last.value.toFixed(2)) : "—");
      stats.appendChild(element);
    }
    draw(data);
  }

  async function refresh() {
    if (!historyUrl || !dialog.open || controller) return;
    const requestedUrl = historyUrl;
    const requestedPeriod = period;
    const active = new AbortController();
    controller = active;
    try {
      const response = await fetch(requestedUrl + "?period=" + encodeURIComponent(requestedPeriod),
        {credentials: "same-origin", cache: "no-store", signal: active.signal});
      if (response.status === 401) {
        location.href = "/login";
        return;
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "История Zabbix недоступна");
      if (dialog.open && historyUrl === requestedUrl && period === requestedPeriod) show(data);
    } catch (error) {
      if (error.name !== "AbortError" && dialog.open
          && historyUrl === requestedUrl && period === requestedPeriod) {
        status.textContent = error.message;
      }
    } finally {
      if (controller === active) controller = null;
    }
  }

  for (const button of periodButtons) {
    button.addEventListener("click", () => {
      if (period === button.dataset.period) return;
      period = button.dataset.period;
      for (const option of periodButtons) {
        option.setAttribute("aria-pressed", String(option === button));
      }
      if (controller) {
        controller.abort();
        controller = null;
      }
      latest = null;
      stats.replaceChildren();
      clearChart();
      status.textContent = "Загрузка истории Zabbix…";
      refresh();
      scheduleRefresh();
    });
  }

  function openHistory(label, url) {
    historyUrl = url;
    latest = null;
    title.textContent = label;
    status.textContent = "Загрузка истории Zabbix…";
    stats.replaceChildren();
    clearChart();
    dialog.showModal();
    refresh();
    scheduleRefresh();
  }

  if (list) list.addEventListener("click", event => {
    const row = event.target.closest(".modem-row");
    if (!row || !list.contains(row)) return;
    openHistory(row.querySelector("span").textContent,
      baseUrl + "/modem-channels/" + encodeURIComponent(row.dataset.itemId) + "/history");
  });
  if (opticalList) opticalList.addEventListener("click", event => {
    const row = event.target.closest(".optical-row");
    if (!row || !opticalList.contains(row)) return;
    openHistory(row.querySelector("span").textContent,
      baseUrl + "/optical-power/" + encodeURIComponent(row.dataset.itemId) + "/history");
  });
  if (restartButton) restartButton.addEventListener("click", () => {
    if (!restartButton.dataset.itemId) return;
    openHistory("Рестарты/ч", baseUrl + "/modem-restarts/"
      + encodeURIComponent(restartButton.dataset.itemId) + "/history");
  });
  pingLoss.addEventListener("click", () => {
    openHistory("Потери пакетов Zabbix", baseUrl + "/ping-loss/history");
  });
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    clearInterval(timer);
    timer = null;
    if (controller) controller.abort();
    historyUrl = null;
  });
  window.addEventListener("resize", () => { if (dialog.open && latest) draw(latest); });
})();
