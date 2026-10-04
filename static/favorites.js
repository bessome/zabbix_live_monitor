(() => {
  const csrf = document.querySelector('.site-header input[name="csrf_token"]');
  if (!csrf) return;

  function showState(button, favorite) {
    button.dataset.favorite = favorite ? "true" : "false";
    button.setAttribute("aria-pressed", String(favorite));
    button.textContent = favorite ? "★" : "☆";
    const label = favorite ? "Убрать из избранного" : "Добавить в избранное";
    button.setAttribute("aria-label", label);
    button.title = label;
  }

  document.addEventListener("click", async event => {
    const button = event.target.closest(".favorite-toggle");
    if (!button || button.disabled) return;
    const favorite = button.dataset.favorite === "true";
    button.disabled = true;
    try {
      const body = new FormData();
      body.append("csrf_token", csrf.value);
      body.append("action", favorite ? "remove" : "add");
      const response = await fetch("/api/favorites/"
        + encodeURIComponent(button.dataset.category) + "/"
        + encodeURIComponent(button.dataset.hostId),
        {method: "POST", credentials: "same-origin", body});
      if (response.status === 401) { location.href = "/login"; return; }
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || data.detail || "Не удалось изменить избранное");
      if (document.getElementById("my-devices")) { location.reload(); return; }
      document.querySelectorAll(".favorite-toggle").forEach(other => {
        if (other.dataset.hostId === button.dataset.hostId) showState(other, data.favorite);
      });
    } catch (error) {
      window.alert(error.message);
    } finally {
      button.disabled = false;
    }
  });
})();
