(function () {
  const root = document.querySelector("[data-notice-root]");
  if (!root) return;

  const toggle = root.querySelector(".notice-slip-toggle");
  const sheet = root.querySelector(".notice-sheet");
  const line = root.querySelector("[data-notice-line]");
  const dot = root.querySelector("[data-notice-dot]");
  const count = root.querySelector("[data-notice-count]");
  const items = root.querySelector("[data-notice-items]");
  let open = false;

  function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function render(data) {
    const notices = data.notices || [];
    const unread = data.unread || 0;
    const newest = notices[0];
    line.textContent = newest ? newest.message : "暂无任务动态";
    dot.hidden = unread < 1;
    count.hidden = unread < 1;
    count.textContent = unread > 0 ? String(unread) : "";
    toggle.setAttribute("aria-label", unread > 0 ? "实验室通知，" + unread + " 条未读" : "实验室通知");
    items.replaceChildren();
    if (!notices.length) {
      const empty = document.createElement("li");
      empty.className = "notice-empty";
      empty.textContent = "没有通知";
      items.append(empty);
      return;
    }
    notices.forEach((notice) => {
      const item = document.createElement("li");
      if (!notice.read) item.classList.add("is-unread");
      const link = document.createElement("a");
      link.href = notice.href || "/history";
      if (!notice.read) {
        const mark = document.createElement("i");
        mark.className = "notice-dot";
        mark.setAttribute("aria-hidden", "true");
        link.append(mark);
      }
      const text = document.createElement("span");
      text.textContent = notice.message;
      link.append(text);
      item.append(link);
      items.append(item);
    });
  }

  async function load() {
    try {
      const response = await fetch("/api/notices", { credentials: "same-origin" });
      if (response.status === 401) return;
      if (!response.ok) return;
      render(await response.json());
    } catch (_error) {
      line.textContent = "通知暂时没有取到";
    }
  }

  async function markRead() {
    try {
      await fetch("/api/notices/read", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrfToken() },
      });
    } catch (_error) {
      return;
    }
    await load();
  }

  function setOpen(next) {
    open = next;
    sheet.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) return;
    sheet.focus();
    markRead();
  }

  toggle.addEventListener("click", () => setOpen(!open));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !open) return;
    setOpen(false);
    toggle.focus();
  });
  document.addEventListener("click", (event) => {
    if (open && !root.contains(event.target)) setOpen(false);
  });

  load();
  window.setInterval(load, 15000);
})();
