(() => {
  for (const root of document.querySelectorAll("[data-modem-reboot-url]")) {
    const button = root.querySelector("button");
    const message = root.querySelector("[role=status]");
    button.addEventListener("click", async () => {
      if (button.disabled || !confirm("Перезагрузить модем " + root.dataset.deviceLabel
        + "? Связь ненадолго прервётся.")) return;
      button.disabled = true;
      message.textContent = "Отправка команды…";
      const form = new FormData();
      form.set("csrf_token", root.dataset.csrfToken);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 45000);
      try {
        const response = await fetch(root.dataset.modemRebootUrl, {
          method: "POST", body: form, credentials: "same-origin",
          cache: "no-store", signal: controller.signal
        });
        if (response.status === 401) {
          location.href = "/login";
          return;
        }
        const data = await response.json();
        message.textContent = response.ok ? data.message : (data.error || data.detail || "Команда не принята.");
      } catch (error) {
        message.textContent = "Нет ответа сервера. Результат перезагрузки неизвестен; проверьте пинг и статус.";
      } finally {
        clearTimeout(timer);
        button.title = "Повторная команда будет доступна через 30 секунд.";
        setTimeout(() => {
          button.disabled = false;
          button.title = "";
        }, 30000);
      }
    });
  }
})();
