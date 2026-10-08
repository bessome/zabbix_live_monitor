(() => {
  const root = document.getElementById("ping-monitor");
  if (!root) return;
  const canvas = document.getElementById("ping-canvas");
  const context = canvas.getContext("2d");
  const fields = {
    current: document.getElementById("ping-current"),
    min: document.getElementById("ping-min"),
    avg: document.getElementById("ping-avg"),
    max: document.getElementById("ping-max"),
    loss: document.getElementById("ping-loss"),
    uptime: document.getElementById("ping-uptime"),
    state: document.getElementById("ping-state"),
    message: document.getElementById("ping-message")
  };
  const currentUnit = root.querySelector(".ping-unit");
  const url = (root.dataset.apiBase || ("/api/devices/" + encodeURIComponent(root.dataset.category)
    + "/" + encodeURIComponent(root.dataset.hostId))) + "/ping";
  const uptimeUrl = url.replace(/\/ping$/, "/uptime");
  let latest = null;
  let inFlight = false;
  let uptimeInFlight = false;

  function millis(value) {
    return value == null ? "—" : Number(value).toFixed(value < 10 ? 1 : 0);
  }

  function formatUptime(seconds) {
    if (!Number.isInteger(seconds) || seconds < 0) return "n/a";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor(seconds % 86400 / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    if (days) return days + "d " + hours + "h";
    if (hours) return hours + "h " + minutes + "m";
    if (minutes) return minutes + "m";
    return seconds + "s";
  }

  function draw(data) {
    const dark = document.documentElement.dataset.theme === "dark";
    const colors = dark ? {
      background: "#111d2a", grid: "#40566a", text: "#c2d0df",
      line: "#69b8f1", loss: "#ff7770", lossShade: "rgba(255, 119, 112, 0.22)"
    } : {
      background: "#ffffff", grid: "#e2e9f0", text: "#657588",
      line: "#176cad", loss: "#cb3934", lossShade: "rgba(205, 58, 54, 0.15)"
    };
    const width = Math.max(1, canvas.clientWidth);
    const height = Math.max(1, canvas.clientHeight);
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.fillStyle = colors.background;
    context.fillRect(0, 0, width, height);
    const left = 52, right = 40, top = 11, bottom = 20;
    const plotWidth = Math.max(1, width - left - right);
    const plotHeight = Math.max(1, height - top - bottom);
    const yBottom = top + plotHeight;
    const values = data.samples.filter(sample => sample.ms != null).map(sample => sample.ms);
    const peak = Math.max(0, ...values);
    const maxValue = [10, 20, 50, 100, 250, 500].find(limit => peak <= limit)
      || Math.ceil(peak / 250) * 250;
    context.font = "11px system-ui, sans-serif";
    context.textBaseline = "middle";
    context.lineWidth = 1;
    for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
      const y = yBottom - fraction * plotHeight;
      context.strokeStyle = colors.grid;
      context.beginPath();
      context.moveTo(left, Math.round(y) + 0.5);
      context.lineTo(width - right, Math.round(y) + 0.5);
      context.stroke();
      context.fillStyle = colors.text;
      context.textAlign = "left";
      context.fillText(Number((maxValue * fraction).toFixed(maxValue <= 20 ? 1 : 0)) + " ms", 3, y);
    }
    context.fillStyle = colors.loss;
    context.textAlign = "right";
    for (const [fraction, label] of [[1, "100%"], [0.5, "50%"], [0, "0%"]]) {
      context.fillText(label, width - 2, yBottom - fraction * plotHeight);
    }
    context.fillStyle = colors.text;
    context.textBaseline = "alphabetic";
    context.textAlign = "left";
    context.fillText("−60s", left, height - 3);
    context.textAlign = "center";
    context.fillText("−30s", left + plotWidth / 2, height - 3);
    context.textAlign = "right";
    context.fillText("now", width - right, height - 3);
    const start = data.server_time - 60;
    function xFor(sample) {
      return left + Math.max(0, Math.min(1, (sample.at - start) / 60)) * plotWidth;
    }
    for (const sample of data.samples) {
      if (sample.ms !== null) continue;
      const x = xFor(sample);
      context.fillStyle = colors.lossShade;
      context.fillRect(x - 2, top, 4, plotHeight);
      context.fillStyle = colors.loss;
      context.fillRect(x - 2, top, 4, 8);
    }
    if (data.avg_ms != null) {
      const averageY = yBottom - Math.min(data.avg_ms / maxValue, 1) * plotHeight;
      context.save();
      context.strokeStyle = colors.text;
      context.setLineDash([5, 4]);
      context.beginPath();
      context.moveTo(left, averageY);
      context.lineTo(width - right, averageY);
      context.stroke();
      context.restore();
    }
    context.beginPath();
    context.strokeStyle = colors.line;
    context.lineWidth = 2.5;
    context.lineJoin = "round";
    let previous = null;
    for (const sample of data.samples) {
      if (sample.ms == null) {
        previous = null;
        continue;
      }
      const x = xFor(sample);
      const y = yBottom - (sample.ms / maxValue) * plotHeight;
      if (previous && sample.at - previous.at <= 1.8) context.lineTo(x, y);
      else context.moveTo(x, y);
      previous = sample;
    }
    context.stroke();
    if (previous) {
      context.fillStyle = colors.line;
      context.beginPath();
      context.arc(xFor(previous), yBottom - (previous.ms / maxValue) * plotHeight, 3.5, 0, 2 * Math.PI);
      context.fill();
    }
    if (!data.samples.length) {
      context.fillStyle = colors.text;
      context.textAlign = "center";
      context.fillText("Ожидание первого ответа…", left + plotWidth / 2, top + plotHeight / 2);
      context.textAlign = "left";
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
      if (!response.ok) throw new Error(data.error || "Пинг недоступен");
      latest = data;
      fields.current.textContent = data.sent && data.current_ms == null ? "×" : millis(data.current_ms);
      fields.current.title = data.sent && data.current_ms == null ? "Последний запрос потерян" : "";
      currentUnit.hidden = data.current_ms == null;
      fields.min.textContent = millis(data.min_ms);
      fields.avg.textContent = millis(data.avg_ms);
      fields.max.textContent = millis(data.max_ms);
      fields.loss.textContent = data.loss_pct + "%";
      fields.loss.title = data.lost + " потеряно из " + data.sent + " запросов";
      fields.state.textContent = data.error ? "Ошибка" : !data.sent ? "Ожидание…" : data.current_ms == null ? "Нет ответа" : "Доступен";
      fields.state.className = "ping-state " + (data.error ? "ping-down" : !data.sent ? "" : data.current_ms == null ? "ping-down" : "ping-up");
      fields.message.textContent = data.error || (!data.sent ? "Ожидание первого результата…" : "");
      draw(data);
    } catch (error) {
      fields.state.textContent = "Ошибка";
      fields.state.className = "ping-state ping-down";
      fields.message.textContent = error.message;
    } finally {
      inFlight = false;
    }
  }

  async function refreshUptime() {
    if (uptimeInFlight) return;
    uptimeInFlight = true;
    try {
      const response = await fetch(uptimeUrl, {credentials: "same-origin", cache: "no-store"});
      if (response.status === 401) {
        location.href = "/login";
        return;
      }
      if (!response.ok) throw new Error("SNMP недоступен");
      const data = await response.json();
      fields.uptime.textContent = formatUptime(data.seconds);
    } catch (_) {
      fields.uptime.textContent = "n/a";
    } finally {
      uptimeInFlight = false;
    }
  }

  window.addEventListener("resize", () => { if (latest) draw(latest); });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      refresh();
      refreshUptime();
    }
  });
  refresh();
  setInterval(refresh, 1000);
  refreshUptime();
  setInterval(refreshUptime, 10000);
})();
