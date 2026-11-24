window.createLogViewer = function createLogViewer(log, options) {
  const getJobId = options.getJobId;
  const onUnauthorized = options.onUnauthorized || null;
  let logOffset = 0;
  let stick = true;

  if (log) {
    log.addEventListener("scroll", () => {
      stick = log.scrollHeight - log.scrollTop - log.clientHeight <= 12;
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
    if (log) log.textContent = "";
  }

  return { refresh, reset };
};
