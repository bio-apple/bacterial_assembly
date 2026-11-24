(() => {
  const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  document.querySelectorAll("time[data-timestamp]").forEach((time) => {
    const date = new Date(time.dataset.timestamp);
    if (!Number.isNaN(date.getTime())) time.textContent = dateFormatter.format(date);
  });

  const formatDuration = (seconds) => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    if (hours) return `${hours}小时 ${minutes}分`;
    if (minutes) return `${minutes}分 ${remainder}秒`;
    return `${remainder}秒`;
  };

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  document.querySelectorAll("[data-cancel-job]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (window.confirmAction) {
        const accepted = await window.confirmAction("取消任务");
        if (!accepted) return;
      }
      button.disabled = true;
      const response = await fetch(`/api/jobs/${encodeURIComponent(button.dataset.cancelJob)}/cancel`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf || "" },
      });
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      window.location.reload();
    });
  });

  document.querySelectorAll("[data-rerun-job]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (window.confirmAction) {
        const accepted = await window.confirmAction("重新分析");
        if (!accepted) return;
      }
      button.disabled = true;
      const response = await fetch(`/api/jobs/${encodeURIComponent(button.dataset.rerunJob)}/rerun`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf || "", Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      if (!response.ok) {
        button.disabled = false;
        const note = button.parentElement.querySelector(".history-rerun-error");
        if (note) note.textContent = payload.error || "不能重新分析。";
        return;
      }
      window.location.reload();
    });
  });

  document.querySelectorAll("[data-live-duration]").forEach((duration) => {
    const initialSeconds = Number(duration.dataset.elapsedSeconds);
    const started = performance.now();
    const update = () => {
      const elapsed = initialSeconds + Math.floor((performance.now() - started) / 1000);
      duration.textContent = formatDuration(elapsed);
    };
    update();
    window.setInterval(update, 1000);
  });
})();
