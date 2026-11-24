(() => {
  const root = document.querySelector("#job-monitor");
  if (!root) return;

  const jobId = root.dataset.jobId;
  const software = document.querySelector("#current-software");
  const status = document.querySelector("#job-status");
  const stageLabel = document.querySelector("#current-stage");
  const stageProgress = document.querySelector("#stage-progress");
  const elapsed = document.querySelector("#elapsed");
  const updated = document.querySelector("#stage-updated");
  const rail = document.querySelector("#stage-rail");
  const errorNode = document.querySelector("#job-error");
  const download = document.querySelector("#result-download");
  const cancelButton = document.querySelector("#cancel-job");
  const partialNote = document.querySelector("#partial-note");
  const log = document.querySelector("#job-log");
  const csrf = document.querySelector('meta[name="csrf-token"]').content;

  const statusLabels = {
    queued: "排队中",
    running: "分析中",
    cancelling: "取消中",
    cancelled: "已取消",
    succeeded: "已完成",
    failed: "失败",
  };
  const stateLabels = {
    waiting: "等待",
    running: "进行中",
    done: "完成",
    failed: "失败",
  };

  let logOffset = 0;
  let elapsedTimer = null;
  const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

  function formatDuration(seconds) {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    if (hours) return `${hours}小时 ${minutes}分`;
    if (minutes) return `${minutes}分 ${remainder}秒`;
    return `${remainder}秒`;
  }

  function formatTimestamp(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return dateFormatter.format(date);
  }

  function appendText(parent, className, text) {
    const node = document.createElement("span");
    node.className = className;
    node.textContent = text;
    parent.append(node);
    return node;
  }

  function renderRail(groups) {
    rail.replaceChildren();
    groups.forEach((group) => {
      const item = document.createElement("li");
      item.className = `stage-method state-${group.state}`;
      item.dataset.method = group.id;
      const node = document.createElement("span");
      node.className = "stage-node";
      node.setAttribute("aria-hidden", "true");
      const body = document.createElement("div");
      const head = document.createElement("div");
      head.className = "stage-method-head";
      const title = document.createElement("strong");
      title.textContent = group.title;
      appendText(head, "stage-state", stateLabels[group.state] || group.state);
      head.prepend(title);
      const steps = document.createElement("ol");
      steps.className = "stage-substeps";
      for (const step of group.steps) {
        const stepItem = document.createElement("li");
        stepItem.className = `stage-step state-${step.state}`;
        stepItem.dataset.stageId = step.id;
        appendText(stepItem, "stage-step-label", step.label);
        const meta = document.createElement("span");
        meta.className = "stage-step-meta";
        if (step.percent !== null && step.percent !== undefined) {
          appendText(meta, "stage-percent", `${step.percent}%`);
        }
        appendText(meta, "stage-state", stateLabels[step.state] || step.state);
        stepItem.append(meta);
        if (step.detail) {
          const detail = document.createElement("small");
          detail.textContent = step.detail;
          stepItem.append(detail);
        }
        steps.append(stepItem);
      }
      body.append(head, steps);
      item.append(node, body);
      rail.append(item);
    });
  }

  function bindElapsed(job) {
    if (elapsedTimer) {
      window.clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
    const seconds = job.progress.elapsed_seconds;
    if (seconds === null || seconds === undefined) {
      elapsed.textContent = "等待开始";
      return;
    }
    const running = job.status === "running" || job.status === "cancelling";
    const started = performance.now();
    const draw = () => {
      const extra = running ? Math.floor((performance.now() - started) / 1000) : 0;
      elapsed.textContent = formatDuration(seconds + extra);
    };
    draw();
    if (running) elapsedTimer = window.setInterval(draw, 1000);
  }

  function render(job) {
    const progress = job.progress;
    const current = progress.current || {};
    software.textContent = current.software || "尚未开始";
    status.className = `status-pill status-${job.status}`;
    status.textContent = statusLabels[job.status] || job.status;
    stageLabel.textContent = current.label || (job.status === "queued" ? "等待开始" : "尚未进入阶段");
    stageProgress.textContent =
      current.percent === null || current.percent === undefined ? "未报告" : `${current.percent}%`;
    updated.textContent = formatTimestamp(progress.updated_at);
    updated.dateTime = progress.updated_at || "";
    if (errorNode) {
      errorNode.hidden = !job.error;
      errorNode.textContent = job.error || "";
    }
    if (download) {
      download.hidden = !job.archive_available;
      download.textContent = job.status === "failed" || job.status === "cancelled"
        ? "下载部分结果"
        : "下载分析结果";
    }
    if (cancelButton) {
      cancelButton.hidden = !(job.status === "queued" || job.status === "running");
      cancelButton.disabled = false;
    }
    if (partialNote) {
      partialNote.hidden = !(job.status === "cancelled" && !job.archive_available);
    }
    renderRail(progress.groups || []);
    bindElapsed(job);
  }

  async function refreshJob() {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
      credentials: "same-origin",
    });
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    if (!response.ok) return;
    const data = await response.json();
    if (data.job) render(data.job);
  }

  async function refreshLog() {
    const response = await fetch(
      `/api/jobs/${encodeURIComponent(jobId)}/log?offset=${logOffset}`,
      { credentials: "same-origin" }
    );
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    if (!response.ok) return;
    const data = await response.json();
    if (data.offset < logOffset) {
      log.textContent = "";
      logOffset = 0;
    }
    if (data.text) {
      log.textContent += data.text;
      logOffset = data.offset;
    }
  }

  if (cancelButton) {
    cancelButton.addEventListener("click", async () => {
      cancelButton.disabled = true;
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf },
      });
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      await refreshJob();
    });
  }

  refreshJob();
  refreshLog();
  window.setInterval(() => {
    refreshJob();
    refreshLog();
  }, 2500);
})();
