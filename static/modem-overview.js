(() => {
  const root = document.getElementById("modem-monitor");
  if (!root) return;
  const open = document.getElementById("modem-overview-open");
  const dialog = document.getElementById("modem-overview-dialog");
  const close = document.getElementById("modem-overview-close");
  const canvas = document.getElementById("modem-overview-canvas");
  const status = document.getElementById("modem-overview-status");
  const legend = document.getElementById("modem-overview-legend");
  const buttons = [...document.querySelectorAll("#modem-overview-periods button")];
  const url = "/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId) + "/modem-overview/history";
  const timezone = document.documentElement.dataset.timezone;
  const hourFormat = new Intl.DateTimeFormat("ru-RU", {
    timeZone: timezone, hour: "2-digit", minute: "2-digit"
  });
  const dateFormat = new Intl.DateTimeFormat("ru-RU", {
    timeZone: timezone, day: "2-digit", month: "2-digit",
    hour: "2-digit", minute: "2-digit"
  });
  const refreshIntervals = {"1h": 30000, "12h": 60000, "24h": 120000,
    "2d": 120000, "14d": 300000};
  const lightColors = {us: "#c23c46", ds_level: "#176cad", ds_snr: "#148054",
    error_rate: "#a76700", loss: "#8054ba"};
  const darkColors = {us: "#ff858c", ds_level: "#79bfff", ds_snr: "#74d5a0",
    error_rate: "#ffc04d", loss: "#c9a1ff"};
  let period = "1h";
  let latest = null;
  let controller = null;
  let timer = null;

  function formatValue(value) {
    return Number(value.toFixed(2)).toString();
  }

  function clearChart() {
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  }

  function axisRange(points, fromZero = false) {
    if (!points.length) return {low: 0, high: 1};
    const values = points.map(point => point.value);
    const low = fromZero ? 0 : Math.min(...values);
    const high = Math.max(...values);
    if (fromZero) return {low, high: Math.max(1, high * 1.1)};
    const padding = Math.max((high - low) * 0.1, Math.abs(high) * 0.02, 0.1);
    return {low: low - padding, high: high + padding};
  }

  function axisLabel(value) {
    if (Math.abs(value) >= 1000) return formatValue(value / 1000) + "k";
    return formatValue(value);
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
    const colors = dark ? darkColors : lightColors;
    const grid = dark ? "#40566a" : "#dce6ef";
    const text = dark ? "#adbdcc" : "#607185";
    const left = 47, right = 72, top = 22, bottom = 28;
    const plotWidth = Math.max(1, width - left - right);
    const plotHeight = Math.max(1, height - top - bottom);
    const plotRight = left + plotWidth;
    const plotBottom = top + plotHeight;
    const x = time => left + (time - data.from) / (data.to - data.from) * plotWidth;
    const byKey = Object.fromEntries(data.series.map(series => [series.key, series]));
    const levelPoints = ["us", "ds_level"].flatMap(key => byKey[key]?.points || []);
    const snrPoints = byKey.ds_snr?.points || [];
    const errorPoints = byKey.error_rate?.points || [];
    const levelAxis = axisRange(levelPoints);
    const snrAxis = axisRange(snrPoints);
    const errorAxis = axisRange(errorPoints, true);
    const y = (value, axis) => top + (axis.high - value)
      / (axis.high - axis.low) * plotHeight;
    ctx.font = "11px system-ui, sans-serif";
    if (levelPoints.length) {
      ctx.fillStyle = text;
      ctx.textAlign = "left";
      ctx.fillText(byKey.us?.units || byKey.ds_level?.units || "dBmV", 3, 12);
    }
    if (snrPoints.length) {
      ctx.fillStyle = colors.ds_snr;
      ctx.textAlign = "left";
      ctx.fillText("SNR", plotRight + 4, 12);
    }
    if (errorPoints.length) {
      ctx.fillStyle = colors.error_rate;
      ctx.textAlign = "right";
      ctx.fillText("Err/s", width - 2, 12);
    }
    for (let i = 0; i <= 4; i++) {
      const yy = top + i / 4 * plotHeight;
      ctx.strokeStyle = grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(left, yy);
      ctx.lineTo(plotRight, yy);
      ctx.stroke();
      const fraction = 1 - i / 4;
      if (levelPoints.length) {
        ctx.fillStyle = text;
        ctx.textAlign = "right";
        ctx.fillText(axisLabel(levelAxis.low + fraction
          * (levelAxis.high - levelAxis.low)), left - 5, yy + 4);
      }
      if (snrPoints.length) {
        ctx.fillStyle = colors.ds_snr;
        ctx.textAlign = "left";
        ctx.fillText(axisLabel(snrAxis.low + fraction
          * (snrAxis.high - snrAxis.low)), plotRight + 4, yy + 4);
      }
      if (errorPoints.length) {
        ctx.fillStyle = colors.error_rate;
        ctx.textAlign = "right";
        ctx.fillText(axisLabel(errorAxis.low + fraction
          * (errorAxis.high - errorAxis.low)), width - 2, yy + 4);
      }
    }
    const tickCount = period === "1h" || width >= 500 ? 4 : 2;
    const timeFormat = period === "1h" ? hourFormat : dateFormat;
    for (let i = 0; i <= tickCount; i++) {
      const xx = left + i / tickCount * plotWidth;
      ctx.fillStyle = text;
      ctx.textAlign = i === 0 ? "left" : i === tickCount ? "right" : "center";
      ctx.fillText(timeFormat.format(new Date(
        (data.from + i / tickCount * (data.to - data.from)) * 1000)),
      xx, height - 8);
    }
    const loss = byKey.loss;
    if (loss?.points.length) {
      const bucketCount = Math.max(1, Math.min(width < 500 ? 8 : 24,
        Math.floor(plotWidth / (width < 500 ? 21 : 30))));
      const buckets = Array(bucketCount).fill(null);
      for (const point of loss.points) {
        const index = Math.max(0, Math.min(bucketCount - 1,
          Math.floor((point.time - data.from) / (data.to - data.from) * bucketCount)));
        buckets[index] = buckets[index] == null ? point.value
          : Math.max(buckets[index], point.value);
      }
      const bucketWidth = plotWidth / bucketCount;
      ctx.font = "10px system-ui, sans-serif";
      ctx.textAlign = "center";
      buckets.forEach((value, index) => {
        if (value == null || value <= 0) return;
        const middle = left + (index + 0.5) * bucketWidth;
        const barHeight = Math.max(2, Math.max(0, Math.min(100, value)) / 100 * plotHeight);
        ctx.fillStyle = colors.loss;
        ctx.globalAlpha = 0.35;
        ctx.fillRect(middle - bucketWidth * 0.35, plotBottom - barHeight,
          bucketWidth * 0.7, barHeight);
        ctx.globalAlpha = 1;
        ctx.fillText(formatValue(value), middle,
          Math.max(top + 9, plotBottom - barHeight - 4));
      });
      ctx.font = "11px system-ui, sans-serif";
    }
    for (const series of data.series.filter(item => item.key !== "loss")) {
      const points = series.points;
      if (!points.length) continue;
      const axis = series.key === "ds_snr" ? snrAxis
        : series.key === "error_rate" ? errorAxis : levelAxis;
      const gap = Math.max(300, 3 * (data.to - data.from) / points.length);
      ctx.strokeStyle = colors[series.key];
      ctx.lineWidth = 2;
      ctx.beginPath();
      points.forEach((point, index) => {
        const xx = x(point.time), yy = y(point.value, axis);
        if (index === 0 || point.time - points[index - 1].time > gap) ctx.moveTo(xx, yy);
        else ctx.lineTo(xx, yy);
      });
      ctx.stroke();
      const last = points[points.length - 1];
      ctx.fillStyle = colors[series.key];
      ctx.beginPath();
      ctx.arc(x(last.time), y(last.value, axis), 3, 0, 2 * Math.PI);
      ctx.fill();
    }
    if (!data.series.some(series => series.points.length)) {
      ctx.fillStyle = text;
      ctx.textAlign = "center";
      ctx.fillText("Нет данных за выбранный период", left + plotWidth / 2,
        top + plotHeight / 2);
    }
  }

  function show(data) {
    latest = data;
    const unavailable = [...data.missing, ...data.series
      .filter(series => !series.points.length).map(series => series.label)];
    const messages = [];
    if (data.series.some(series => series.key === "loss" && series.points.length)) {
      messages.push("Loss: столбики, максимум за интервал, %");
    }
    if (data.series.some(series => series.aggregation === "hourly_average")) {
      messages.push("14d — почасовое среднее Zabbix");
    }
    if (unavailable.length) messages.push("Нет данных: " + unavailable.join(", "));
    status.textContent = messages.join(" · ");
    canvas.setAttribute("aria-label", "Общий график модема за " + data.period
      + ": " + data.series.map(series => series.label).join(", "));
    const colors = document.documentElement.dataset.theme === "dark"
      ? darkColors : lightColors;
    legend.replaceChildren();
    for (const series of data.series) {
      const row = document.createElement("div");
      row.className = "modem-overview-legend-item";
      const swatch = document.createElement("i");
      if (series.key === "loss") swatch.className = "loss-swatch";
      swatch.style.backgroundColor = colors[series.key];
      const name = document.createElement("span");
      name.textContent = series.label;
      const value = document.createElement("strong");
      const last = series.points.at(-1);
      value.textContent = last ? formatValue(last.value)
        + (series.units ? " " + series.units : "") : "—";
      row.append(swatch, name, value);
      legend.appendChild(row);
    }
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
    timer = setInterval(refresh, refreshIntervals[period]);
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
