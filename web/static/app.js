(() => {
  const form = document.querySelector("#job-form");
  if (!form) return;

  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const list = document.querySelector("#job-list");
  const count = document.querySelector("#queue-count");
  const message = document.querySelector("#form-message");
  const uploadProgress = document.querySelector("#upload-progress");
  const uploadProgressBar = document.querySelector("#upload-progress-bar");
  const uploadProgressLabel = document.querySelector("#upload-progress-label");
  const uploadProgressPercent = document.querySelector("#upload-progress-percent");
  const uploadProgressDetail = document.querySelector("#upload-progress-detail");
  const log = document.querySelector("#job-log");
  const detail = document.querySelector("#detail-summary");
  const status = document.querySelector("#detail-status");
  const download = document.querySelector("#download-link");
  const start = document.querySelector("#start-button");
  const spadesOptions = document.querySelector("#spades-options");
  const assemblerOrderField = document.querySelector("#assembler-order");
  const selectionOrderLabel = document.querySelector("#selection-order");
  let selected = null;
  let logOffset = 0;
  let jobsById = new Map();
  let assemblerOrder = [];

  const labels = {
    queued: "排队中",
    running: "分析中",
    succeeded: "已完成",
    failed: "失败",
  };

  function showMessage(text, isError = true) {
    message.textContent = text;
    message.classList.toggle("message-success", !isError);
  }

  function formatBytes(bytes) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value)} ${units[unit]}`;
  }

  function showUploadProgress(loaded, total, lengthComputable) {
    uploadProgress.hidden = false;
    if (!lengthComputable || total <= 0) {
      uploadProgressBar.removeAttribute("value");
      uploadProgressLabel.textContent = "正在传输 R1 / R2 FASTQ";
      uploadProgressPercent.textContent = "";
      uploadProgressDetail.textContent = `已发送 ${formatBytes(loaded)}`;
      return;
    }

    const transferComplete = loaded >= total;
    const percentage = Math.min(100, Math.round((loaded / total) * 100));
    uploadProgressBar.value = percentage;
    uploadProgressPercent.textContent = `${percentage}%`;
    uploadProgressLabel.textContent = transferComplete
      ? "文件已传输，正在加入队列…"
      : "正在传输 R1 / R2 FASTQ";
    uploadProgressDetail.textContent = `已发送 ${formatBytes(loaded)} / ${formatBytes(total)}`;
  }

  function uploadJob(formData) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("POST", "/api/jobs");
      request.withCredentials = true;
      request.setRequestHeader("X-CSRF-Token", csrf);
      request.upload.addEventListener("progress", (event) => {
        showUploadProgress(event.loaded, event.total, event.lengthComputable);
      });
      request.addEventListener("load", () => {
        if (request.status === 401) {
          window.location.assign("/login");
          reject(new Error("登录状态已失效，请重新登录。"));
          return;
        }
        let data;
        try {
          data = JSON.parse(request.responseText);
        } catch (_error) {
          reject(new Error(`服务器响应无法识别（HTTP ${request.status}）。`));
          return;
        }
        if (request.status < 200 || request.status >= 300) {
          reject(new Error(data.error || "无法提交任务。"));
          return;
        }
        resolve(data);
      });
      request.addEventListener("error", () => {
        reject(new Error("网络中断，文件未能完整上传，请重试。"));
      });
      request.addEventListener("abort", () => {
        reject(new Error("上传已中断，请重新提交文件。"));
      });
      request.send(formData);
    });
  }

  function renderJobs(jobs) {
    jobsById = new Map(jobs.map((job) => [job.id, job]));
    count.textContent = String(jobs.length);
    list.replaceChildren();
    if (!jobs.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "还没有分析任务。提交一个 FASTQ 样本开始。";
      list.append(empty);
      return;
    }
    for (const job of jobs) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `job-item${job.id === selected ? " selected" : ""}`;
      button.dataset.jobId = job.id;
      const top = document.createElement("span");
      top.className = "job-topline";
      const title = document.createElement("span");
      title.className = "job-name";
      title.textContent = `${job.sample} · ${job.id.slice(0, 8)}`;
      const state = document.createElement("span");
      state.className = `status-pill status-${job.status}`;
      state.textContent = labels[job.status] || job.status;
      top.append(title, state);
      const bottom = document.createElement("span");
      bottom.className = "job-subline";
      const tools = document.createElement("span");
      tools.className = "job-assemblers";
      for (const assembler of job.assemblers) {
        const chip = document.createElement("span");
        chip.textContent = assembler;
        tools.append(chip);
      }
      const time = document.createElement("span");
      time.className = "job-time";
      time.textContent = `${job.threads} 线程 / 组装器 · ${(job.created_at || "").replace("T", " ").slice(0, 16)}`;
      bottom.append(tools, time);
      button.append(top, bottom);
      button.addEventListener("click", () => selectJob(job.id));
      list.append(button);
    }
  }

  async function refreshJobs() {
    const response = await fetch("/api/jobs", { credentials: "same-origin" });
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    if (!response.ok) return;
    const data = await response.json().catch(() => ({
      error: "上传请求失败，请检查反向代理设置和服务器磁盘空间。",
    }));
    renderJobs(data.jobs);
    if (selected && jobsById.has(selected)) renderDetail(jobsById.get(selected));
  }

  function renderDetail(job) {
    status.className = `status-pill status-${job.status}`;
    status.textContent = labels[job.status] || job.status;
    detail.textContent = `${job.assemblers.join(" / ")} · 每种方法 ${job.threads} 线程`;
    if (job.error) detail.textContent += ` · ${job.error}`;
    download.hidden = !job.archive_available;
    download.textContent = job.status === "failed" ? "下载部分结果 ↓" : "下载分析结果 ↓";
    download.href = `/api/jobs/${encodeURIComponent(job.id)}/download`;
  }

  async function refreshLog() {
    if (!selected) return;
    const response = await fetch(
      `/api/jobs/${encodeURIComponent(selected)}/log?offset=${logOffset}`,
      { credentials: "same-origin" }
    );
    if (!response.ok) return;
    const data = await response.json();
    if (data.offset < logOffset) {
      log.textContent = "";
      logOffset = 0;
    }
    if (data.text) {
      log.textContent += data.text;
      logOffset = data.offset;
      log.scrollTop = log.scrollHeight;
    }
  }

  function selectJob(jobId) {
    selected = jobId;
    logOffset = 0;
    log.textContent = "";
    const job = jobsById.get(jobId);
    if (job) renderDetail(job);
    refreshJobs();
    refreshLog();
  }

  for (const input of form.querySelectorAll('input[type="file"]')) {
    input.addEventListener("change", () => {
      const name = input.files[0]?.name || "选择 FASTQ 文件";
      input.closest(".file-lane").querySelector(".file-name").textContent = name;
    });
  }

  form.querySelectorAll('input[name="assembler"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) {
        assemblerOrder = assemblerOrder.filter((item) => item !== input.value);
        assemblerOrder.push(input.value);
      } else {
        assemblerOrder = assemblerOrder.filter((item) => item !== input.value);
      }
      assemblerOrderField.value = JSON.stringify(assemblerOrder);
      selectionOrderLabel.textContent = assemblerOrder.length
        ? `执行顺序：${assemblerOrder.join(" / ")}`
        : "选择顺序即执行顺序。";
      spadesOptions.hidden = !form.querySelector('input[name="assembler"][value="spades"]').checked;
    });
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const selectedAssemblers = [...form.querySelectorAll('input[name="assembler"]:checked')];
    if (!selectedAssemblers.length) {
      showMessage("请至少选择一种组装器。");
      return;
    }
    start.disabled = true;
    showMessage("正在上传文件并加入队列…", false);
    try {
      uploadProgress.hidden = true;
      uploadProgressBar.value = 0;
      const data = await uploadJob(new FormData(form));
      showMessage("任务已加入串行队列。", false);
      uploadProgress.hidden = true;
      form.querySelectorAll('input[type="file"]').forEach((input) => {
        input.value = "";
        input.closest(".file-lane").querySelector(".file-name").textContent = "选择 FASTQ 文件";
      });
      form.reset();
      assemblerOrder = [];
      assemblerOrderField.value = "[]";
      selectionOrderLabel.textContent = "选择顺序即执行顺序。";
      spadesOptions.hidden = true;
      selected = data.job.id;
      logOffset = 0;
      log.textContent = "";
      await refreshJobs();
    } catch (error) {
      uploadProgress.hidden = true;
      showMessage(error.message);
    } finally {
      start.disabled = false;
    }
  });

  refreshJobs();
  window.setInterval(() => {
    refreshJobs();
    refreshLog();
  }, 2500);
})();
