window.createLogViewer = function createLogViewer(log, options) {
  const getJobId = options.getJobId;
  const onUnauthorized = options.onUnauthorized || null;
  let logOffset = 0;
  let stick = true;

  const toolbar = log && log.parentElement
    ? log.parentElement.querySelector("[data-log-toolbar]")
    : null;
  const followToggle = toolbar ? toolbar.querySelector("[data-log-follow]") : null;
  const copyButton = toolbar ? toolbar.querySelector("[data-log-copy]") : null;

  if (followToggle) {
    followToggle.checked = true;
    followToggle.addEventListener("change", () => {
      stick = followToggle.checked;
      if (stick && log) log.scrollTop = log.scrollHeight;
    });
  }

  if (log) {
    log.addEventListener("scroll", () => {
      const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight <= 12;
      stick = nearBottom;
      if (followToggle) followToggle.checked = nearBottom;
    });
  }

  if (copyButton && log) {
    copyButton.addEventListener("click", async () => {
      const text = log.textContent || "";
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const area = document.createElement("textarea");
          area.value = text;
          document.body.appendChild(area);
          area.select();
          document.execCommand("copy");
          area.remove();
        }
        copyButton.textContent = "已复制";
        setTimeout(() => {
          copyButton.textContent = "复制日志";
        }, 1200);
      } catch (error) {
        copyButton.textContent = "复制失败";
        setTimeout(() => {
          copyButton.textContent = "复制日志";
        }, 1200);
      }
    });
  }

  async function refresh() {
    const jobId = getJobId();
    if (!log || !jobId) return;
    const response = await fetch(
      `/api/jobs/${encodeURIComponent(jobId)}/log?offset=${logOffset}`,
      { credentials: "same-origin" }
    );
    if (response.status === 401) {
      if (onUnauthorized) onUnauthorized();
      return;
    }
    if (!response.ok) return;
    const data = await response.json();
    if (data.offset < logOffset) {
      log.textContent = "";
      logOffset = 0;
    }
    if (!data.text) return;
    const follow = stick;
    log.textContent += data.text;
    logOffset = data.offset;
    if (follow) log.scrollTop = log.scrollHeight;
  }

  function reset() {
    logOffset = 0;
    stick = true;
    if (followToggle) followToggle.checked = true;
    if (log) log.textContent = "";
  }

  return { refresh, reset };
};
