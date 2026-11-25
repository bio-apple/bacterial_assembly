(function () {
  const toggle = document.querySelector("#sidebar-toggle");
  const sidebar = document.querySelector("#app-sidebar");
  const backdrop = document.querySelector("#sidebar-backdrop");
  if (!toggle || !sidebar) return;

  function setOpen(open) {
    document.body.classList.toggle("sidebar-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    if (backdrop) backdrop.hidden = !open;
  }

  toggle.addEventListener("click", () => {
    setOpen(!document.body.classList.contains("sidebar-open"));
  });
  if (backdrop) {
    backdrop.addEventListener("click", () => setOpen(false));
  }
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });
})();
