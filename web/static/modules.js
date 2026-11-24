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
})();
