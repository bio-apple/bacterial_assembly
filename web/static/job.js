(() => {
  const root = document.querySelector("[data-job-monitor]");
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

  const statusLabels = window.JOB_STATUS_LABELS;
  const stateLabels = {
    waiting: "等待",
    running: "进行中",
    done: "完成",
    failed: "失败",
  };
  const logViewer = window.createLogViewer(log, {
    getJobId: () => jobId,
    onUnauthorized: () => window.location.assign("/login"),
  });

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
        appendText(stepItem, "stage-step-label", step.summary || step.label || step.id);
        const meta = document.createElement("span");
        meta.className = "stage-step-meta";
        if (step.percent !== null && step.percent !== undefined) {
          appendText(meta, "stage-percent", `${step.percent}%`);
        }
        appendText(meta, "stage-state", stateLabels[step.state] || step.state);
        stepItem.append(meta);
        if (step.started_at || step.finished_at) {
          const times = document.createElement("span");
          times.className = "stage-step-times";
          const bits = [];
          if (step.started_at) bits.push(`开始 ${formatTimestamp(step.started_at)}`);
          if (step.finished_at) bits.push(`结束 ${formatTimestamp(step.finished_at)}`);
          times.textContent = bits.join(" · ");
          stepItem.append(times);
        }
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

  function renderPhase(job) {
    const phase = document.querySelector("#phase-times");
    const started = document.querySelector("#phase-started");
    const cancel = document.querySelector("#phase-cancel");
    const archived = document.querySelector("#phase-archived");
    const beat = document.querySelector("#worker-beat");
    if (phase && started && cancel && archived) {
      phase.hidden = !(job.started_at || job.cancel_requested_at || job.archived_at);
      started.textContent = job.started_at ? formatTimestamp(job.started_at) : "尚未开始";
      cancel.textContent = job.cancel_requested_at ? formatTimestamp(job.cancel_requested_at) : "没有";
      archived.textContent = job.archived_at ? formatTimestamp(job.archived_at) : "尚未发布下载";
    }
    if (!beat) return;
    const live = job.status === "running" || job.status === "cancelling";
    beat.hidden = !live;
    if (!live) return;
    const attempt = job.attempt || 1;
    const group = job.process_group_id ? ` · 进程组 ${job.process_group_id}` : "";
    beat.textContent = (job.heartbeat_at
      ? `第 ${attempt} 次执行 · 最近心跳 ${formatTimestamp(job.heartbeat_at)}`
      : `第 ${attempt} 次执行`) + group;
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
      download.textContent = job.status === "partial_success" || job.status === "failed" || job.status === "cancelled"
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
    renderPhase(job);
    renderRail(progress.groups || []);
    bindElapsed(job);
    if (job.detail) {
      renderResources(job.detail.resources);
      renderQc(job);
      renderSteps(job.detail.steps);
      renderComparison(job.detail);
      renderReports(job.detail.report_ready);
      refreshHost(job.status);
    }
  }

  function renderResources(resources) {
    const threads = document.querySelector("#assigned-threads");
    const memory = document.querySelector("#assigned-memory");
    if (!resources || !threads || !memory) return;
    threads.textContent = resources.threads_text || "";
    memory.textContent = resources.memory_text || "";
  }

  function append(parent, tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    parent.append(node);
    return node;
  }

  function renderQc(job) {
    const section = document.querySelector("#qc-summary");
    const detail = job.detail || {};
    const qc = detail.qc;
    if (!section || !qc) return;
    const link = section.querySelector("#fastp-html");
    if (link) link.hidden = !detail.fastp_html;
    section.querySelectorAll(".qc-sheet, #qc-note").forEach((node) => node.remove());
    if (qc.ready) {
      const sheet = append(section, "div", "qc-sheet");
      sheet.id = "qc-sheet";
      const axis = append(sheet, "p", "qc-axis");
      append(axis, "span", "", "过滤前");
      append(axis, "span", "", "过滤后");
      for (const row of qc.rows || []) {
        const line = append(sheet, "div", "qc-row");
        append(line, "p", "qc-label", row.label);
        line.append(qcSide(row.before, row.before_width, false));
        line.append(qcSide(row.after, row.after_width, true));
      }
    }
    if (qc.note) append(section, "p", "report-note", qc.note).id = "qc-note";
  }

  function qcSide(side, width, after) {
    const cell = document.createElement("div");
    cell.className = "qc-side";
    const value = document.createElement("strong");
    if (!side || !side.text) {
      value.className = "qc-missing";
      value.textContent = "未记录";
      cell.append(value);
      return cell;
    }
    value.textContent = side.text;
    cell.append(value);
    if (side.raw) append(cell, "span", "checksum", `记录值 ${side.raw}`);
    if (width !== null && width !== undefined) {
      const track = append(cell, "span", "qc-track");
      const fill = append(track, "span", after ? "qc-fill is-after" : "qc-fill");
      fill.style.width = `${width}%`;
    }
    return cell;
  }

  function renderSteps(steps) {
    const body = document.querySelector("#step-ledger-body");
    const empty = document.querySelector("#step-ledger-empty");
    const table = document.querySelector("#step-ledger-table");
    if (!body || !empty || !table) return;
    const rows = Array.isArray(steps) ? steps : [];
    body.replaceChildren();
    empty.hidden = rows.length > 0;
    table.hidden = rows.length === 0;
    body.hidden = rows.length === 0;
    const labels = window.JOB_STATUS_LABELS || {};
    for (const step of rows) {
      const line = document.createElement("tr");
      const name = document.createElement("th");
      name.scope = "row";
      name.textContent = step.label || step.name || "";
      if (step.error) {
        const reason = document.createElement("small");
        reason.textContent = step.error;
        name.append(reason);
      }
      if (step.exit_code !== null && step.exit_code !== undefined) {
        append(name, "span", "checksum", `退出码 ${step.exit_code}`);
      }
      const state = document.createElement("td");
      const pill = document.createElement("span");
      pill.className = `status-pill status-${step.status}`;
      pill.textContent = labels[step.status] || step.status;
      state.append(pill);
      const started = document.createElement("td");
      started.className = "mono";
      started.textContent = step.started_at ? formatTimestamp(step.started_at) : "—";
      const ended = document.createElement("td");
      ended.className = "mono";
      ended.textContent = step.completed_at ? formatTimestamp(step.completed_at) : "—";
      const duration = document.createElement("td");
      duration.className = "mono";
      duration.textContent = formatStepDuration(step.started_at, step.completed_at) || "—";
      line.append(name, state, started, ended, duration);
      body.append(line);
    }
  }

  function formatStepDuration(startedAt, endedAt) {
    if (!startedAt || !endedAt) return "";
    const start = Date.parse(startedAt);
    const end = Date.parse(endedAt);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
    const seconds = Math.floor((end - start) / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    if (hours) return `${hours}小时 ${minutes}分`;
    if (minutes) return `${minutes}分 ${secs}秒`;
    return `${secs}秒`;
  }

  function renderComparison(detail) {
    const section = document.querySelector("#assembly-comparison");
    if (!section || !detail) return;
    const views = Array.isArray(detail.comparisons) && detail.comparisons.length
      ? detail.comparisons
      : (detail.comparison ? [detail.comparison] : []);
    if (!views.length) return;
    const previous = section.querySelector('input[name="length-scope"]:checked');
    const selectedScope = previous ? previous.value : "original";
    section.replaceChildren();
    const head = append(section, "div", "monitor-section-head");
    append(head, "h2", "", "组装器比较").id = "comparison-title";
    append(head, "p", "", "只列出这次选择的组装方法。一次只看一个长度口径。");
    const scopes = append(section, "fieldset", "length-scopes");
    append(scopes, "legend", "", "长度口径");
    for (const view of views) {
      const label = append(scopes, "label", "", "");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "length-scope";
      input.value = view.scope || "original";
      input.checked = input.value === selectedScope;
      label.append(input, document.createTextNode(view.scope_label || input.value));
    }
    if (![...scopes.querySelectorAll("input")].some((input) => input.checked)) {
      scopes.querySelector("input").checked = true;
    }
    for (const view of views) {
      const panel = append(section, "div", "compare-panel");
      panel.dataset.scope = view.scope || "original";
      append(panel, "p", "report-note", view.note || "");
      if (!view.ready) {
        append(panel, "p", "report-note", view.message || "比较尚未就绪。");
        continue;
      }
      const scroll = append(panel, "div", "compare-scroll");
      const table = append(scroll, "table", "compare-table metric-sheet");
      const thead = append(table, "thead");
      const header = append(thead, "tr");
      append(header, "th", "", "指标").scope = "col";
      for (const column of view.columns || []) {
        append(header, "th", "", column.label || column.id || "").scope = "col";
      }
      const body = append(table, "tbody");
      for (const metric of view.metrics || []) {
        const row = append(body, "tr");
        append(row, "th", "", metric.name || "").scope = "row";
        for (const cell of metric.cells || []) append(row, "td", "", cell.text || "");
      }
      append(table, "caption", "", view.caption || "");
      if ((view.missing_labels || []).length) {
        append(panel, "p", "assembly-missing", `这次选择了、但这一口径没有记录：${view.missing_labels.join("、")}。`);
      }
      if ((view.extra_labels || []).length) {
        append(panel, "p", "assembly-missing", `比较表里还有这次没有选择的组装方法：${view.extra_labels.join("、")}。未列入上表。`);
      }
      const charts = append(panel, "div", "metric-charts");
      for (const chart of view.charts || []) {
        const figure = append(charts, "figure", "metric-chart");
        append(figure, "figcaption", "", chart.title || "");
        if ((chart.bars || []).length) {
          const bars = append(figure, "div", "metric-bars");
          for (const bar of chart.bars) {
            const line = append(bars, "div", "metric-bar-row");
            append(line, "span", "metric-bar-name", bar.label || "");
            const track = append(line, "span", "metric-bar-track");
            track.setAttribute("aria-hidden", "true");
            const fill = append(track, "span", "metric-bar-fill");
            const width = Number(bar.width);
            if (Number.isFinite(width) && width >= 0 && width <= 100) fill.style.width = `${width}%`;
            append(line, "span", "metric-bar-value", bar.text || "");
          }
        } else {
          append(figure, "p", "report-note", chart.empty || "这项指标没有可绘制的数值。");
        }
      }
    }
    append(section, "p", "compare-rule", views[0].rule || "不要仅根据 N50 自动推荐最佳组装结果。");
  }

  function renderReports(ready) {
    const readyNode = document.querySelector("[data-report-ready]");
    const waitingNode = document.querySelector("[data-report-waiting]");
    if (readyNode) readyNode.hidden = !ready;
    if (waitingNode) waitingNode.hidden = Boolean(ready);
  }

  function formatHostCpu(metric) {
    if (!metric || metric.ok !== true || typeof metric.percent !== "number") return "未能读取";
    return typeof metric.cores === "number" ? `${metric.percent}% · ${metric.cores} 核` : `${metric.percent}%`;
  }

  function formatHostMemory(metric) {
    if (!metric || metric.ok !== true || typeof metric.percent !== "number") return "未能读取";
    if (typeof metric.used === "string" && typeof metric.total === "string") {
      return `${metric.percent}% · ${metric.used} / ${metric.total}`;
    }
    return `${metric.percent}%`;
  }

  async function refreshHost(status) {
    const note = document.querySelector("#host-usage-note");
    const metrics = document.querySelector("#host-usage-metrics");
    const cpu = document.querySelector("#host-cpu");
    const memory = document.querySelector("#host-memory");
    if (!note || !metrics || !cpu || !memory) return;
    const hostBlock = document.querySelector("#host-usage");
    const live = status === "running" || status === "cancelling";
    if (!live) {
      if (hostBlock) hostBlock.hidden = true;
      metrics.hidden = true;
      cpu.textContent = "";
      memory.textContent = "";
      return;
    }
    if (hostBlock) hostBlock.hidden = false;
    note.textContent = "此刻这台机器的占用，不是本任务的历史曲线。";
    try {
      const response = await fetch("/api/host", { credentials: "same-origin" });
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      if (!response.ok) throw new Error("host");
      const data = await response.json();
      const host = data.host || {};
      cpu.textContent = formatHostCpu(host.cpu);
      memory.textContent = formatHostMemory(host.memory);
      metrics.hidden = false;
    } catch (error) {
      cpu.textContent = "";
      memory.textContent = "";
      metrics.hidden = true;
      note.textContent = "当前主机占用未能读取。";
    }
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
    await logViewer.refresh();
  }

  const rerunButton = document.querySelector("#rerun-job");
  const rerunError = document.querySelector("#rerun-error");
  if (rerunButton) {
    rerunButton.addEventListener("click", async () => {
      if (window.confirmAction) {
        const accepted = await window.confirmAction("重新分析");
        if (!accepted) return;
      }
      rerunButton.disabled = true;
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/rerun`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf, Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      if (!response.ok || !payload.job) {
        rerunButton.disabled = false;
        if (rerunError) rerunError.textContent = payload.error || "不能重新分析。";
        return;
      }
      window.location.assign(`/jobs/${encodeURIComponent(payload.job.id)}`);
    });
  }

  if (cancelButton) {
    cancelButton.addEventListener("click", async () => {
      if (window.confirmAction) {
        const accepted = await window.confirmAction("取消任务");
        if (!accepted) return;
      }
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

  document.querySelectorAll("[data-step-duration]").forEach((node) => {
    const text = formatStepDuration(node.dataset.started, node.dataset.ended);
    if (text) node.textContent = text;
  });

  refreshJob();
  refreshLog();
  window.setInterval(() => {
    refreshJob();
    refreshLog();
  }, 2500);
})();
