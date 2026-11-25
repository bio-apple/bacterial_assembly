(() => {
  const form = document.querySelector("#job-form");
  if (!form) return;

  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const list = document.querySelector("#job-list");
  const count = document.querySelector("#queue-count");
  const message = document.querySelector("#form-message");
  const readyLine = document.querySelector("#transfer-ready");
  const log = document.querySelector("#job-log");
  const detail = document.querySelector("#detail-summary");
  const status = document.querySelector("#detail-status");
  const download = document.querySelector("#download-link");
  const monitor = document.querySelector("#monitor-link");
  const start = document.querySelector("#start-button");
  const spadesOptions = document.querySelector("#spades-options");
  const assemblerOrderField = document.querySelector("#assembler-order");
  const selectionOrderLabel = document.querySelector("#selection-order");
  let selected = new URLSearchParams(window.location.search).get("job");
  let jobsById = new Map();
  let assemblerOrder = [];
  const labels = window.JOB_STATUS_LABELS;
  const jobTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const logViewer = window.createLogViewer(log, {
    getJobId: () => selected,
  });

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

  function rotr(value, bits) {
    return ((value >>> bits) | (value << (32 - bits))) >>> 0;
  }

  function Sha256() {
    this.h = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ]);
    this.k = new Uint32Array([
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
      0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
      0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
      0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
      0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    ]);
    this.block = new Uint8Array(64);
    this.blockLength = 0;
    this.bytes = 0;
  }

  Sha256.prototype.compress = function compress(block) {
    const w = new Uint32Array(64);
    for (let i = 0; i < 16; i += 1) {
      const j = i * 4;
      w[i] = ((block[j] << 24) | (block[j + 1] << 16) | (block[j + 2] << 8) | block[j + 3]) >>> 0;
    }
    for (let i = 16; i < 64; i += 1) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let a = this.h[0];
    let b = this.h[1];
    let c = this.h[2];
    let d = this.h[3];
    let e = this.h[4];
    let f = this.h[5];
    let g = this.h[6];
    let h = this.h[7];
    for (let i = 0; i < 64; i += 1) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = ((e & f) ^ (~e & g)) >>> 0;
      const temp1 = (h + s1 + ch + this.k[i] + w[i]) >>> 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
      const temp2 = (s0 + maj) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    this.h[0] = (this.h[0] + a) >>> 0;
    this.h[1] = (this.h[1] + b) >>> 0;
    this.h[2] = (this.h[2] + c) >>> 0;
    this.h[3] = (this.h[3] + d) >>> 0;
    this.h[4] = (this.h[4] + e) >>> 0;
    this.h[5] = (this.h[5] + f) >>> 0;
    this.h[6] = (this.h[6] + g) >>> 0;
    this.h[7] = (this.h[7] + h) >>> 0;
  };

  Sha256.prototype.update = function update(data) {
    let offset = 0;
    while (offset < data.length) {
      const take = Math.min(64 - this.blockLength, data.length - offset);
      this.block.set(data.subarray(offset, offset + take), this.blockLength);
      this.blockLength += take;
      offset += take;
      if (this.blockLength === 64) {
        this.compress(this.block);
        this.bytes += 64;
        this.blockLength = 0;
      }
    }
  };

  Sha256.prototype.hex = function hex() {
    const totalBits = (this.bytes + this.blockLength) * 8;
    const padded = new Uint8Array(this.blockLength < 56 ? 64 : 128);
    padded.set(this.block.subarray(0, this.blockLength));
    padded[this.blockLength] = 0x80;
    const view = new DataView(padded.buffer);
    view.setUint32(padded.length - 8, Math.floor(totalBits / 0x100000000));
    view.setUint32(padded.length - 4, totalBits >>> 0);
    this.compress(padded.subarray(0, 64));
    if (padded.length === 128) this.compress(padded.subarray(64));
    return Array.from(this.h, (word) => word.toString(16).padStart(8, "0")).join("");
  };

  const uploadLabels = {
    pending: "等待上传",
    uploading: "上传中",
    completed: "可分析",
    failed: "上传失败",
    consumed: "已使用",
  };
  const kmerMessage = "参数校验错误。SPAdes k-mer 必须是不超过 127 的奇数，并用逗号分隔。";
  const notReadyMessage = "两份 FASTQ 都通过字节校验和记录校验后，才能运行组装。";
  let submitting = false;

  function bindLane(role) {
    return {
      role,
      input: document.querySelector(`#${role}-file`),
      root: document.querySelector(`[data-lane="${role}"]`),
      fill: document.querySelector(`#${role}-fill`),
      track: document.querySelector(`#${role}-track`),
      name: document.querySelector(`#${role}-transfer-name`),
      state: document.querySelector(`#${role}-state`),
      percent: document.querySelector(`#${role}-percent`),
      detail: document.querySelector(`#${role}-detail`),
      resume: document.querySelector(`#${role}-resume`),
      status: "pending",
      generation: 0,
      xhr: null,
      sessionId: null,
      file: null,
      fileName: "",
      loadedPercent: 0,
      detailText: "选择 FASTQ 文件",
      checksumFailed: false,
    };
  }

  const lanes = { r1: bindLane("r1"), r2: bindLane("r2") };

  // Only client-side suffix check for the paired FASTQ uploader.
  function isFastq(name) {
    return /\.(fastq\.gz|fq\.gz|fastq|fq)$/i.test(name);
  }

  function filesMatch(left, right) {
    return Boolean(
      left && right && left.name === right.name && left.size === right.size && left.lastModified === right.lastModified
    );
  }

  function storageKey(role, file) {
    return `bacterial-assembly-upload:${role}:${file.name}:${file.size}:${file.lastModified}`;
  }

  function renderLane(lane) {
    const percent = lane.status === "completed" ? 100 : lane.loadedPercent || 0;
    lane.root.classList.toggle("is-uploading", lane.status === "uploading");
    lane.root.classList.toggle("is-done", lane.status === "completed");
    lane.root.classList.toggle("is-failed", lane.status === "failed");
    lane.fill.style.width = `${percent}%`;
    lane.track.setAttribute("aria-valuenow", String(percent));
    lane.percent.textContent = `${percent}%`;
    lane.state.textContent = uploadLabels[lane.status] || uploadLabels.pending;
    lane.state.className = `transfer-state state-${lane.status}`;
    lane.name.textContent = lane.fileName || "尚未选择";
    lane.detail.textContent = lane.detailText;
    lane.resume.hidden = lane.status !== "failed" || lane.checksumFailed;
  }

  function updateReady() {
    const ready = lanes.r1.status === "completed" && lanes.r2.status === "completed";
    start.disabled = submitting || !ready;
    start.title = ready ? "" : notReadyMessage;
    readyLine.classList.toggle("is-ready", ready);
    readyLine.textContent = ready ? "可以开始分析" : notReadyMessage;
  }

  function resetLane(lane) {
    lane.generation += 1;
    if (lane.xhr) lane.xhr.abort();
    lane.xhr = null;
    lane.status = "pending";
    lane.sessionId = null;
    lane.loadedPercent = 0;
    lane.detailText = "选择 FASTQ 文件";
    lane.checksumFailed = false;
    renderLane(lane);
    updateReady();
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: { "X-CSRF-Token": csrf, ...(options.headers || {}) },
    });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("登录状态已失效，请重新登录。");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || `上传失败（HTTP ${response.status}）。`);
      error.checksum = data.code === "checksum_mismatch" || /SHA-256|校验不一致/.test(error.message);
      error.format = data.code === "fastq_invalid";
      throw error;
    }
    return data;
  }

  function putChunk(lane, uploadId, index, bytes, generation, onProgress) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      lane.xhr = request;
      request.open("PUT", `/api/uploads/${encodeURIComponent(uploadId)}/chunks/${index}`);
      request.withCredentials = true;
      request.setRequestHeader("X-CSRF-Token", csrf);
      request.setRequestHeader("Content-Type", "application/octet-stream");
      request.upload.addEventListener("progress", (event) => {
        if (generation === lane.generation) onProgress(event.loaded || 0);
      });
      request.addEventListener("load", () => {
        if (lane.xhr === request) lane.xhr = null;
        if (generation !== lane.generation) {
          reject(Object.assign(new Error("aborted"), { aborted: true }));
          return;
        }
        if (request.status === 401) {
          window.location.assign("/login");
          reject(new Error("登录状态已失效，请重新登录。"));
          return;
        }
        let data = {};
        try {
          data = JSON.parse(request.responseText);
        } catch (_error) {
          data = {};
        }
        if (request.status < 200 || request.status >= 300) {
          reject(new Error(data.error || `分片上传失败（HTTP ${request.status}）。`));
          return;
        }
        resolve(data);
      });
      request.addEventListener("error", () => {
        if (lane.xhr === request) lane.xhr = null;
        reject(new Error("网络中断。点「继续上传」，从缺失分片继续。"));
      });
      request.addEventListener("abort", () => {
        if (lane.xhr === request) lane.xhr = null;
        reject(Object.assign(new Error("aborted"), { aborted: true }));
      });
      request.send(bytes);
    });
  }

  async function ensureSession(lane, file) {
    const key = storageKey(lane.role, file);
    const existing = sessionStorage.getItem(key);
    if (existing) {
      const response = await fetch(`/api/uploads/${encodeURIComponent(existing)}`, { credentials: "same-origin" });
      if (response.status === 401) {
        window.location.assign("/login");
        throw new Error("登录状态已失效，请重新登录。");
      }
      if (response.ok) {
        const data = await response.json();
        const upload = data.upload;
        if (upload && upload.filename === file.name && upload.size === file.size && upload.status !== "consumed") {
          lane.sessionId = upload.id;
          return upload;
        }
      }
      sessionStorage.removeItem(key);
    }
    const created = await api("/api/uploads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, size: file.size, role: lane.role }),
    });
    sessionStorage.setItem(key, created.upload.id);
    lane.sessionId = created.upload.id;
    return created.upload;
  }

  function speedLabel(bytesPerSecond) {
    if (!bytesPerSecond) return "";
    return `${formatBytes(bytesPerSecond)}/s`;
  }

  async function runLane(lane, file) {
    const generation = ++lane.generation;
    if (lane.xhr) lane.xhr.abort();
    lane.file = file;
    lane.fileName = file.name;
    lane.checksumFailed = false;
    lane.status = "uploading";
    lane.detailText = "正在连接";
    renderLane(lane);
    updateReady();
    try {
      const upload = await ensureSession(lane, file);
      if (generation !== lane.generation) return;
      if (upload.status === "completed") {
        lane.sessionId = upload.id;
        lane.status = "completed";
        lane.loadedPercent = 100;
        lane.detailText = "字节校验和记录校验已通过";
        renderLane(lane);
        updateReady();
        return;
      }
      const received = new Set(upload.received || []);
      const chunkSize = upload.chunk_size;
      const hasher = new Sha256();
      const attemptStart = performance.now();
      let attemptBytes = 0;
      for (let index = 0; index < upload.chunk_count; index += 1) {
        if (generation !== lane.generation) return;
        const startByte = index * chunkSize;
        const endByte = Math.min(file.size, startByte + chunkSize);
        const bytes = new Uint8Array(await file.slice(startByte, endByte).arrayBuffer());
        if (generation !== lane.generation) return;
        hasher.update(bytes);
        if (received.has(index)) continue;
        lane.status = "uploading";
        await putChunk(lane, upload.id, index, bytes, generation, (loaded) => {
          const done = startByte + loaded;
          const seconds = (performance.now() - attemptStart) / 1000;
          const speed = seconds >= 0.2 ? (attemptBytes + loaded) / seconds : 0;
          lane.loadedPercent = file.size ? Math.min(99, Math.floor((done / file.size) * 100)) : 0;
          const rate = speedLabel(speed);
          lane.detailText = `${rate ? `${rate} · ` : ""}已传 ${formatBytes(done)} / ${formatBytes(file.size)}`;
          renderLane(lane);
        });
        attemptBytes += bytes.length;
      }
      if (generation !== lane.generation) return;
      lane.status = "uploading";
      lane.loadedPercent = 99;
      lane.detailText = "先核对 SHA-256，再检查 FASTQ 记录";
      renderLane(lane);
      const completed = await api(`/api/uploads/${encodeURIComponent(upload.id)}/complete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sha256: hasher.hex() }),
      });
      if (generation !== lane.generation) return;
      lane.sessionId = completed.upload.id;
      lane.status = "completed";
      lane.loadedPercent = 100;
      lane.detailText = "字节校验和记录校验已通过";
      renderLane(lane);
      updateReady();
    } catch (error) {
      if (generation !== lane.generation || error.aborted) return;
      lane.status = "failed";
      lane.checksumFailed = Boolean(error.checksum || error.format);
      if (error.checksum || error.format) {
        sessionStorage.removeItem(storageKey(lane.role, file));
        lane.sessionId = null;
        lane.detailText = error.message;
      } else {
        lane.detailText = "点「继续上传」，从缺失分片继续。";
      }
      renderLane(lane);
      updateReady();
      showMessage(error.message);
    }
  }

  function clearInput(lane, text) {
    lane.input.value = "";
    lane.input.closest(".file-lane").querySelector(".file-name").textContent = "选择 FASTQ 文件";
    lane.file = null;
    lane.fileName = "";
    if (text) showMessage(text);
    resetLane(lane);
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
      let createdLabel = "";
      if (job.created_at) {
        const created = new Date(job.created_at);
        createdLabel = Number.isNaN(created.getTime())
          ? ""
          : ` · ${jobTimeFormatter.format(created)}`;
      }
      time.textContent = `${job.threads} 线程 / 组装器${createdLabel}`;
      bottom.append(tools, time);
      button.append(top, bottom);
      button.addEventListener("click", () => selectJob(job.id));
      const row = document.createElement("div");
      row.className = "job-row";
      const monitorLink = document.createElement("a");
      monitorLink.className = "job-monitor-link";
      monitorLink.href = `/jobs/${encodeURIComponent(job.id)}`;
      monitorLink.textContent = "查看进度";
      row.append(button, monitorLink);
      list.append(row);
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
    download.textContent = job.status === "failed" || job.status === "cancelled"
      ? "下载部分结果 ↓"
      : "下载分析结果 ↓";
    if (job.status === "cancelled" && !job.archive_available) {
      detail.textContent += " · 运行日志已保留，结果不完整。";
    }
    download.href = `/api/jobs/${encodeURIComponent(job.id)}/download`;
    if (monitor) {
      monitor.hidden = false;
      monitor.href = `/jobs/${encodeURIComponent(job.id)}`;
    }
  }

  async function refreshLog() {
    await logViewer.refresh();
  }

  function selectJob(jobId) {
    selected = jobId;
    const url = new URL(window.location.href);
    if (jobId) url.searchParams.set("job", jobId);
    else url.searchParams.delete("job");
    window.history.replaceState({}, "", url);
    logViewer.reset();
    const job = jobsById.get(jobId);
    if (job) renderDetail(job);
    refreshJobs();
    refreshLog();
  }

  function uploadInFlight() {
    return Object.values(lanes).some((lane) => lane.status === "uploading" || submitting);
  }

  window.addEventListener("beforeunload", (event) => {
    if (!uploadInFlight()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  function onFile(lane) {
    const file = lane.input.files[0];
    const other = lane.role === "r1" ? lanes.r2.input.files[0] : lanes.r1.input.files[0];
    if (!file) {
      lane.file = null;
      lane.fileName = "";
      resetLane(lane);
      return;
    }
    lane.input.closest(".file-lane").querySelector(".file-name").textContent = file.name;
    if (!isFastq(file.name)) {
      clearInput(lane, "只接受 .fastq、.fq、.fastq.gz 或 .fq.gz。");
      return;
    }
    if (!file.size) {
      clearInput(lane, "文件是空的，请重新选择。");
      return;
    }
    if (filesMatch(file, other)) {
      clearInput(lane, "R1 和 R2 必须是两个不同的 FASTQ 文件。");
      return;
    }
    showMessage("", false);
    runLane(lane, file);
  }

  for (const lane of Object.values(lanes)) {
    lane.input.addEventListener("change", () => onFile(lane));
    lane.resume.addEventListener("click", () => {
      const file = lane.input.files[0];
      if (!file) {
        showMessage("请重新选择同一个 FASTQ 文件，即可从缺失分片继续。");
        return;
      }
      runLane(lane, file);
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
    if (lanes.r1.status !== "completed" || lanes.r2.status !== "completed") {
      showMessage(notReadyMessage);
      updateReady();
      return;
    }
    if (assemblerOrder.includes("spades")) {
      const kmers = form.spades_kmers.value.trim();
      if (kmers && !/^[1-9]\d*(,[1-9]\d*)*$/.test(kmers)) {
        showMessage(kmerMessage);
        return;
      }
      if (kmers && kmers.split(",").some((part) => Number(part) > 127 || Number(part) % 2 === 0)) {
        showMessage(kmerMessage);
        return;
      }
    }
    submitting = true;
    updateReady();
    showMessage("正在把样本加入队列…", false);
    try {
      const body = new URLSearchParams();
      body.set("sample", form.sample.value);
      body.set("assembler_order", JSON.stringify(assemblerOrder));
      for (const name of assemblerOrder) body.append("assembler", name);
      body.set("r1_session", lanes.r1.sessionId);
      body.set("r2_session", lanes.r2.sessionId);
      if (assemblerOrder.includes("spades")) {
        const kmers = form.spades_kmers.value.trim();
        if (kmers) body.set("spades_kmers", kmers);
        if (form.spades_careful.checked) body.set("spades_careful", "on");
      }
      const data = await api("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      });
      showMessage("任务已加入串行队列。", false);
      const submittedSample = form.sample.value;
      const knownSamples = document.querySelector("#known-samples");
      if (
        knownSamples
        && submittedSample
        && ![...knownSamples.options].some((option) => option.value === submittedSample)
      ) {
        const option = document.createElement("option");
        option.value = submittedSample;
        knownSamples.append(option);
      }
      for (const lane of Object.values(lanes)) {
        if (lane.file) sessionStorage.removeItem(storageKey(lane.role, lane.file));
        lane.input.closest(".file-lane").querySelector(".file-name").textContent = "选择 FASTQ 文件";
        lane.file = null;
        lane.fileName = "";
        resetLane(lane);
      }
      form.reset();
      assemblerOrder = [];
      assemblerOrderField.value = "[]";
      selectionOrderLabel.textContent = "选择顺序即执行顺序。";
      spadesOptions.hidden = true;
      selected = data.job.id;
      logViewer.reset();
      await refreshJobs();
    } catch (error) {
      showMessage(error.message);
    } finally {
      submitting = false;
      updateReady();
    }
  });

  updateReady();

  refreshJobs();
  window.setInterval(() => {
    refreshJobs();
    refreshLog();
  }, 2500);
})();
