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
