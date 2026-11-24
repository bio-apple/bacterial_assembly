(() => {
  const instrument = document.querySelector("#host-instrument");
  if (!instrument) return;

  function paint(host) {
    for (const meter of instrument.querySelectorAll("[data-meter]")) {
      const metric = host[meter.dataset.meter];
      const percent = meter.querySelector("[data-percent]");
      const detail = meter.querySelector("[data-detail]");
      const bar = meter.querySelector(".host-track i");
      if (!metric || !metric.ok) {
        percent.textContent = "—";
        detail.textContent = "无法读取";
        bar.style.setProperty("--fill", "0%");
        continue;
      }
      percent.textContent = `${metric.percent}%`;
      bar.style.setProperty("--fill", `${metric.percent}%`);
      if (meter.dataset.meter === "cpu") {
        detail.textContent = `${metric.cores} 个逻辑 CPU`;
      } else if (meter.dataset.meter === "disk") {
        detail.textContent = `${metric.used} / ${metric.total} · ${metric.path}`;
      } else {
        detail.textContent = `${metric.used} / ${metric.total}`;
      }
    }
  }

  async function refresh() {
    const response = await fetch("/api/host", { credentials: "same-origin" });
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    if (!response.ok) return;
    const data = await response.json();
    if (data.host) paint(data.host);
  }

  window.setInterval(refresh, 8000);
})();
