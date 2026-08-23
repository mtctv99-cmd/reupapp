const API_BASE = "/api";

let activeJobId = "";
let isPipelineRunning = false;
let progressTimer = null;

let currentSettings = {};
let biliQrKey = "";
let biliPollTimer = null;

// ── Utility ──────────────────────────────────────────────────
function nativeApi() {
  try { return window.pywebview && window.pywebview.api ? window.pywebview.api : null; } catch (e) { return null; }
}

async function uploadFile(file) {
  try {
    const fd = new FormData(); fd.append("file", file);
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    if (!res.ok) return "";
    const data = await res.json(); return data.path || "";
  } catch (e) { return ""; }
}

async function apiFetch(endpoint, options = {}) {
  try {
    const res = await fetch(`${API_BASE}${endpoint}`, {
      ...options, headers: { "Content-Type": "application/json", ...options.headers },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return { error: data.error || data.detail || `HTTP ${res.status}` };
    return data;
  } catch (e) { return { error: e.message || "Network Error" }; }
}

function escapeHtml(str) {
  return String(str || "").replace(/[&<>"']/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

// ── Toast / Loading ──────────────────────────────────────────
function showToast(msg, type = "info", duration = 3000) {
  const c = document.getElementById("toast-container");
  if (!c) return;
  const t = document.createElement("div");
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => t.remove(), duration);
}

function showLoading(msg) {
  const el = document.getElementById("loading-overlay");
  if (el) { el.querySelector(".loading-text").textContent = msg || "Đang xử lý..."; el.classList.remove("hidden"); }
}
function hideLoading() {
  const el = document.getElementById("loading-overlay");
  if (el) el.classList.add("hidden");
}

function showErrorModal(msg) {
  const m = document.getElementById("modal-error");
  const t = document.getElementById("modal-error-msg");
  if (t) t.textContent = msg;
  if (m) m.classList.remove("hidden");
}
function hideErrorModal() {
  const m = document.getElementById("modal-error");
  if (m) m.classList.add("hidden");
}

function showConfirm(msg, withInput) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "modal-overlay";
    const inp = withInput ? '<input type="text" id="confirm-input" style="width:100%;margin-top:8px;background:var(--surface);border:1px solid var(--border-strong);color:#fff;border-radius:var(--radius-sm);padding:7px 10px;font-size:12px;" placeholder="Nhập ở đây..." />' : "";
    const yesLbl = withInput ? "Xác nhận" : "Đồng ý";
    ov.innerHTML = `<div class="modal-card modal-card-confirm"><div class="modal-header"><span class="modal-icon">⚠️</span><h3 class="modal-title-blue">Xác nhận</h3></div><div class="modal-body"><p>${escapeHtml(msg)}</p>${inp}</div><div class="modal-footer confirm-actions"><button class="btn-secondary" id="confirm-no">Hủy</button><button class="btn-primary" id="confirm-yes">${yesLbl}</button></div></div>`;
    document.body.appendChild(ov);
    ov.querySelector("#confirm-no").onclick = () => { ov.remove(); resolve(withInput ? null : false); };
    ov.querySelector("#confirm-yes").onclick = () => {
      const v = withInput ? (ov.querySelector("#confirm-input") || {}).value : true;
      ov.remove(); resolve(withInput ? (v || null) : true);
    };
    ov.addEventListener("click", (e) => { if (e.target === ov) { ov.remove(); resolve(withInput ? null : false); } });
    if (withInput) setTimeout(() => { const i = ov.querySelector("#confirm-input"); if (i) i.focus(); }, 100);
  });
}

// ── DOM Elements ─────────────────────────────────────────────
const statsBadge = document.getElementById("stats-badge");
const inputUrl = document.getElementById("input-url");
const selectAspectRatio = document.getElementById("select-aspect-ratio");
const speedSlider = document.getElementById("speed-slider");
const speedValue = document.getElementById("speed-value");
const volumeSlider = document.getElementById("volume-slider");
const volumeValue = document.getElementById("volume-value");
const selectVoice = document.getElementById("select-voice");
const selectSubStyle = document.getElementById("select-sub-style");
const inputLogoPath = document.getElementById("input-logo-path");
const inputLogoFile = document.getElementById("input-logo-file");
const btnBrowseLogo = document.getElementById("btn-browse-logo");
const selectLogoPos = document.getElementById("select-logo-pos");
const selectLogoOpacity = document.getElementById("select-logo-opacity");
const btnStart = document.getElementById("btn-start");
const btnPaste = document.getElementById("btn-paste");
const btnRefresh = document.getElementById("btn-refresh");
const progressFill = document.getElementById("progress-fill");
const trackerPct = document.getElementById("tracker-pct");
const trackerElapsed = document.getElementById("tracker-elapsed");
const logOutput = document.getElementById("log-output");
const trackerStatus = document.getElementById("tracker-status");
const trackerPanel = document.getElementById("tracker-panel");
const trackerHeaderToggle = document.getElementById("tracker-header-toggle");
const btnToggleTracker = document.getElementById("btn-toggle-tracker");
const btnCancel = document.getElementById("btn-cancel");
const btnOpenConcat = document.getElementById("btn-open-concat");
const btnOpenSettings = document.getElementById("btn-open-settings");
const modalConcat = document.getElementById("modal-concat");
const btnCloseConcatModal = document.getElementById("btn-close-concat-modal");
const concatDropzone = document.getElementById("concat-dropzone");
const btnBrowseConcatFiles = document.getElementById("btn-browse-concat-files");
const inputConcatFiles = document.getElementById("input-concat-files");
const concatFilesListEl = document.getElementById("concat-files-list");
const concatCountEl = document.getElementById("concat-count");
const btnStartConcat = document.getElementById("btn-start-concat");
const concatStatusMsg = document.getElementById("concat-status-msg");
const modalSettings = document.getElementById("modal-settings");
const btnCloseSettings = document.getElementById("btn-close-settings");
const outputVideo = document.getElementById("preview-player") || document.getElementById("output-video");
const outputTitle = document.getElementById("preview-title") || document.getElementById("output-title");
const btnOpenOutputFolder = document.getElementById("btn-open-out-folder");
const inputVideoTitle = document.getElementById("input-video-title");
const btnFetchTitle = document.getElementById("btn-fetch-title");
const inputOutputDir = document.getElementById("input-output-dir");
const btnChooseOutputDir = document.getElementById("btn-choose-output-dir");
const btnOpenFolderQuick = document.getElementById("btn-open-folder-quick");
const btnConcatSelected = document.getElementById("btn-concat-selected");
const selectedCountEl = document.getElementById("selected-count");
let outputFolderPath = "";
let concatFilesList = [];
let selectedJobIds = new Set();

// ── Jobs ─────────────────────────────────────────────────────
async function loadJobs() {
  const res = await apiFetch("/jobs");
  if (!res || !res.data) return;
  const jobs = res.data;
  if (statsBadge) statsBadge.textContent = `${jobs.length} Jobs`;
  const badge = document.getElementById("job-count-badge");
  if (badge) badge.textContent = jobs.length;
  const listEl = document.getElementById("jobs-list");
  if (!listEl) return;
  if (jobs.length === 0) {
    selectedJobIds.clear();
    updateSelectedCount();
    listEl.innerHTML = '<div class="empty-state"><p>Chưa có job nào.</p></div>';
    return;
  }
  listEl.innerHTML = jobs.slice(0, 30).map((j) => `
    <div class="job-item ${j.id === activeJobId ? "active" : ""}" data-job-id="${j.id}">
      <input type="checkbox" class="job-checkbox" data-job-id="${j.id}" ${selectedJobIds.has(j.id) ? "checked" : ""} ${j.status !== "done" ? "disabled style='opacity:0.3;cursor:not-allowed'" : ""} title="${j.status === 'done' ? 'Chọn để ghép' : 'Chỉ ghép được khi hoàn tất'}" />
      <span class="job-item-title" title="${escapeHtml(j.title || j.id)}">${escapeHtml(j.title || j.id)}</span>
      <div class="job-item-actions">
        ${j.status === "done" ? `
          <button class="btn-job-action btn-job-play-native" data-job-id="${j.id}" title="Phát video">▶</button>
          <button class="btn-job-action btn-job-folder-native" data-job-id="${j.id}" title="Mở thư mục chứa video">📂</button>
        ` : ""}
        <span class="job-item-status status-${j.status}">${j.status}</span>
        <button class="btn-job-del-single" data-job-id="${j.id}" title="Xóa">🗑</button>
      </div>
    </div>
  `).join("");

  listEl.querySelectorAll(".job-checkbox").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      e.stopPropagation();
      const id = cb.dataset.jobId;
      if (cb.checked) selectedJobIds.add(id);
      else selectedJobIds.delete(id);
      updateSelectedCount();
    });
    cb.addEventListener("click", (e) => e.stopPropagation());
  });

  listEl.querySelectorAll(".job-item").forEach((el) => {
    el.addEventListener("click", async (e) => {
      if (e.target.closest(".btn-job-del-single") || e.target.closest(".btn-job-action") || e.target.closest(".job-checkbox")) return;
      const id = el.dataset.jobId;
      activeJobId = id;
      listEl.querySelectorAll(".job-item").forEach((c) => c.classList.remove("active"));
      el.classList.add("active");
    });
  });
  listEl.querySelectorAll(".btn-job-play-native").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const jobId = btn.dataset.jobId;
      const res = await apiFetch(`/jobs/output?id=${jobId}`);
      if (res && res.data && res.data.output_path) {
        await apiFetch("/open-file", { method: "POST", body: JSON.stringify({ file_path: res.data.output_path }) });
      } else {
        showToast("Không tìm thấy file video kết quả", "error");
      }
    });
  });
  listEl.querySelectorAll(".btn-job-folder-native").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const jobId = btn.dataset.jobId;
      const res = await apiFetch(`/jobs/output?id=${jobId}`);
      const folder = res?.data?.output_dir || (res?.data?.output_path ? res.data.output_path.substring(0, res.data.output_path.lastIndexOf('\\')) : "");
      if (folder) {
        await apiFetch("/open-folder", { method: "POST", body: JSON.stringify({ folder_path: folder }) });
      } else {
        showToast("Không tìm thấy đường dẫn thư mục", "error");
      }
    });
  });
  listEl.querySelectorAll(".btn-job-del-single").forEach((btn) => {
    btn.addEventListener("click", (e) => { e.stopPropagation(); deleteJob(btn.dataset.jobId); });
  });
  if (jobs.length > 0 && !isPipelineRunning && !activeJobId) {
    activeJobId = jobs[0].id;
    loadOutputPreview(jobs[0]);
  }
}

function updateSelectedCount() {
  if (selectedCountEl) selectedCountEl.textContent = selectedJobIds.size;
  if (btnConcatSelected) {
    btnConcatSelected.style.display = selectedJobIds.size > 0 ? "inline-flex" : "none";
  }
}

async function deleteJob(jobId) {
  if (!(await showConfirm("Xóa job này?"))) return;
  await apiFetch(`/jobs?id=${jobId}`, { method: "DELETE" });
  activeJobId = ""; clearOutput(); loadJobs();
}

// ── Output Preview ───────────────────────────────────────────
async function loadOutputPreview(job) {
  if (!job || !job.id) { clearOutput(); return; }
  const res = await apiFetch(`/jobs/output?id=${job.id}`);
  if (!res || !res.data || !res.data.exists || !res.data.output_path) {
    clearOutput();
    if (outputTitle) outputTitle.textContent = `🎬 ${job.title || job.id} (chưa có video kết quả)`;
    return;
  }
  outputVideo.src = "/media?path=" + encodeURIComponent(res.data.output_path);
  outputVideo.load();
  if (outputTitle) outputTitle.textContent = `🎬 ${job.title || job.id}`;
  outputFolderPath = res.data.output_dir || "";
}

function clearOutput() {
  if (outputVideo) { outputVideo.pause(); outputVideo.removeAttribute("src"); outputVideo.load(); }
  if (outputTitle) outputTitle.textContent = "Chưa có video kết quả";
  outputFolderPath = "";
}

// ── Settings (Bilibili Login) ────────────────────────────────
async function loadSettings() {
  const res = await apiFetch("/settings");
  if (res && res.data) currentSettings = res.data;
  if (currentSettings.voice && selectVoice) {
    const exists = Array.from(selectVoice.options).some(o => o.value === currentSettings.voice);
    if (exists) selectVoice.value = currentSettings.voice;
  }
  if (currentSettings.aspect_ratio && selectAspectRatio) {
    const exists = Array.from(selectAspectRatio.options).some(o => o.value === currentSettings.aspect_ratio);
    if (exists) selectAspectRatio.value = currentSettings.aspect_ratio;
  }
  if (speedSlider && currentSettings.video_speed !== undefined) {
    speedSlider.value = currentSettings.video_speed;
    speedValue.textContent = parseFloat(currentSettings.video_speed).toFixed(2) + "x";
  }
  if (volumeSlider && currentSettings.original_volume !== undefined) {
    volumeSlider.value = currentSettings.original_volume;
    if (volumeValue) volumeValue.textContent = parseInt(currentSettings.original_volume, 10) + "%";
  }
  if (currentSettings.output_dir && inputOutputDir) {
    inputOutputDir.value = currentSettings.output_dir;
    outputFolderPath = currentSettings.output_dir;
  }
  if (currentSettings.sub_style && selectSubStyle) {
    const exists = Array.from(selectSubStyle.options).some(o => o.value === currentSettings.sub_style);
    if (exists) selectSubStyle.value = currentSettings.sub_style;
  }
  if (currentSettings.logo_path && inputLogoPath) inputLogoPath.value = currentSettings.logo_path;
  if (currentSettings.logo_position && selectLogoPos) selectLogoPos.value = currentSettings.logo_position;
  if (currentSettings.logo_opacity !== undefined && selectLogoOpacity) {
    selectLogoOpacity.value = String(parseFloat(currentSettings.logo_opacity));
  }
  checkBilibiliStatus();
}

async function checkBilibiliStatus() {
  const res = await apiFetch("/bilibili/status");
  const icon = document.getElementById("bili-status-icon");
  const text = document.getElementById("bili-status-text");
  const detail = document.getElementById("bili-status-detail");
  const logoutBtn = document.getElementById("btn-bili-logout");
  if (!res || !res.data) return;
  const d = res.data;
  if (d.logged_in) {
    icon.textContent = "✅";
    text.textContent = "Đã đăng nhập Bilibili";
    detail.textContent = d.username || "";
    text.style.color = "var(--ok)";
    logoutBtn.classList.remove("hidden");
  } else {
    icon.textContent = "🔒";
    text.textContent = "Chưa đăng nhập Bilibili";
    detail.textContent = d.message || "Quét QR hoặc dán cookie để đăng nhập";
    text.style.color = "var(--text)";
    logoutBtn.classList.add("hidden");
  }
}

async function createBiliQR() {
  const qrBox = document.getElementById("bili-qr-box");
  const qrImg = document.getElementById("bili-qr-img");
  const qrPlaceholder = document.getElementById("bili-qr-placeholder");
  const qrHint = document.getElementById("bili-qr-hint");
  const qrCountdown = document.getElementById("bili-qr-countdown");
  const btnCreate = document.getElementById("btn-bili-create-qr");

  btnCreate.disabled = true;
  btnCreate.textContent = "⏳ Đang tạo...";

  const res = await apiFetch("/bilibili/qrcode");
  btnCreate.disabled = false;
  btnCreate.textContent = "📱 Tạo QR Code";

  if (!res || !res.data) {
    showToast(res?.error || "Không tạo được QR", "error");
    return;
  }

  biliQrKey = res.data.qrcode_key;
  const qrUrl = res.data.url;

  // Generate QR image using external API
  qrImg.src = `https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=${encodeURIComponent(qrUrl)}`;
  qrImg.classList.remove("hidden");
  qrPlaceholder.classList.add("hidden");
  qrHint.classList.remove("hidden");

  // Start countdown (3 min)
  let remaining = 180;
  qrCountdown.classList.remove("hidden");
  qrCountdown.textContent = `QR hết hạn sau ${remaining}s`;
  if (biliPollTimer) clearInterval(biliPollTimer);

  const countdownInterval = setInterval(() => {
    remaining--;
    qrCountdown.textContent = `QR hết hạn sau ${remaining}s`;
    if (remaining <= 0) {
      clearInterval(countdownInterval);
      if (biliPollTimer) clearInterval(biliPollTimer);
      qrCountdown.textContent = "QR đã hết hạn";
      qrImg.classList.add("hidden");
      qrPlaceholder.classList.remove("hidden");
      qrHint.classList.add("hidden");
    }
  }, 1000);

  // Start polling
  biliPollTimer = setInterval(async () => {
    const pollRes = await apiFetch("/bilibili/poll", {
      method: "POST",
      body: JSON.stringify({ qrcode_key: biliQrKey }),
    });
    if (!pollRes || !pollRes.data) return;
    const st = pollRes.data.status;
    if (st === "scan_success") {
      clearInterval(biliPollTimer);
      clearInterval(countdownInterval);
      qrCountdown.textContent = "✅ Đăng nhập thành công!";
      qrCountdown.style.color = "var(--ok)";
      showToast("Đăng nhập Bilibili thành công!", "success");
      setTimeout(() => {
        qrImg.classList.add("hidden");
        qrPlaceholder.classList.remove("hidden");
        qrHint.classList.add("hidden");
        qrCountdown.classList.add("hidden");
        qrCountdown.style.color = "";
      }, 2000);
      checkBilibiliStatus();
    } else if (st === "expired") {
      clearInterval(biliPollTimer);
      clearInterval(countdownInterval);
      qrCountdown.textContent = "QR đã hết hạn";
      qrImg.classList.add("hidden");
      qrPlaceholder.classList.remove("hidden");
    }
  }, 2000);
}

async function saveBiliCookie() {
  const inp = document.getElementById("bili-cookie-input");
  const cookie = inp.value.trim();
  if (!cookie) { showToast("Vui lòng dán cookie", "error"); return; }
  const res = await apiFetch("/bilibili/cookie", {
    method: "POST",
    body: JSON.stringify({ cookie }),
  });
  if (res && res.status === "ok") {
    showToast("Đã lưu cookie!", "success");
    inp.value = "";
    checkBilibiliStatus();
  } else {
    showToast(res?.error || "Cookie không hợp lệ", "error");
  }
}

async function logoutBili() {
  if (!(await showConfirm("Đăng xuất Bilibili?"))) return;
  await apiFetch("/bilibili/logout", { method: "POST" });
  showToast("Đã đăng xuất", "info");
  checkBilibiliStatus();
}

// ── TikTok TTS accounts ──────────────────────────────────────
async function loadTiktokAccounts() {
  const box = document.getElementById("tiktok-accounts-list");
  if (!box) return;
  const res = await apiFetch("/tiktok/login-status");
  const accounts = res?.accounts || [];
  if (!accounts.length) {
    box.innerHTML = '<p class="text-sub">Chưa có tài khoản nào.</p>';
    return;
  }
  box.innerHTML = "";
  accounts.forEach((a) => {
    const row = document.createElement("div");
    row.className = "flex-between concat-file-item";
    const label = document.createElement("span");
    label.textContent = `🎵 ${a.name || "TikTok"} — ${a.preview} (${a.added_at || ""})`;
    const btn = document.createElement("button");
    btn.className = "btn-danger btn-sm";
    btn.textContent = "🗑️";
    btn.addEventListener("click", async () => {
      await apiFetch(`/tiktok/account?sessionid=${encodeURIComponent(a.sessionid)}`, { method: "DELETE" });
      loadTiktokAccounts();
    });
    row.append(label, btn);
    box.appendChild(row);
  });
}

async function fetchAndTranslateTitle() {
  const url = (inputUrl?.value || "").trim();
  if (!url) {
    return;
  }
  if (btnFetchTitle) {
    btnFetchTitle.disabled = true;
    btnFetchTitle.textContent = "⏳ Đang lấy...";
  }
  if (inputVideoTitle && !inputVideoTitle.value) {
    inputVideoTitle.placeholder = "🔄 Đang lấy & dịch tiêu đề (bạn có thể bấm Khởi Chạy ngay)...";
  }
  try {
    const res = await apiFetch("/fetch-title", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    if (res && res.title && inputVideoTitle) {
      inputVideoTitle.value = res.title;
      showToast(`Đã lấy tiêu đề: ${res.title}`, "success");
    }
  } catch (e) {
    // Không chặn luồng nếu lấy tiêu đề thất bại
  } finally {
    if (btnFetchTitle) {
      btnFetchTitle.disabled = false;
      btnFetchTitle.textContent = "🌐 Tự Lấy & Dịch Tiêu Đề";
    }
    if (inputVideoTitle && !inputVideoTitle.value) {
      inputVideoTitle.placeholder = "Nhập tiêu đề (hoặc để trống để hệ thống tự lấy & dịch khi chạy)...";
    }
  }
}

async function chooseOutputDir() {
  if (btnChooseOutputDir) {
    btnChooseOutputDir.disabled = true;
    btnChooseOutputDir.textContent = "⏳ Đang mở...";
  }
  try {
    const res = await apiFetch("/choose-folder", { method: "POST" });
    if (res && res.folder && inputOutputDir) {
      inputOutputDir.value = res.folder;
      outputFolderPath = res.folder;
      currentSettings.output_dir = res.folder;
      await apiFetch("/settings", { method: "POST", body: JSON.stringify({ output_dir: res.folder }) });
      showToast(`Đã chọn thư mục: ${res.folder}`, "success");
    }
  } catch (e) {
    showToast("Lỗi khi mở hộp thoại chọn thư mục", "error");
  } finally {
    if (btnChooseOutputDir) {
      btnChooseOutputDir.disabled = false;
      btnChooseOutputDir.textContent = "📂 Chọn Thư Mục";
    }
  }
}

async function startPipeline() {
  const url = inputUrl.value.trim();
  if (!url) { showErrorModal("Vui lòng dán link video."); return; }
  if (isUnsupportedPlatform(url)) {
    showErrorModal("Hệ thống không hỗ trợ link YouTube/Facebook."); return;
  }

  const payload = {
    url, source_lang: "zh", target_lang: "vi",
    title: "",
    voice: selectVoice.value.trim(), remove_sub: true,
    aspect_ratio: selectAspectRatio.value,
    sub_style: selectSubStyle ? selectSubStyle.value : "white_black",
    sub_font_size: 38,
    video_speed: parseFloat(speedSlider ? speedSlider.value : 1.0),
    original_volume: parseInt(volumeSlider ? volumeSlider.value : 100, 10),
    output_dir: inputOutputDir ? inputOutputDir.value.trim() : "",
    logo_path: inputLogoPath ? inputLogoPath.value.trim() : "",
    logo_position: selectLogoPos ? selectLogoPos.value : "top_right",
    logo_opacity: parseFloat(selectLogoOpacity ? selectLogoOpacity.value : 0.8),
  };

  const res = await apiFetch("/jobs/create", { method: "POST", body: JSON.stringify(payload) });
  if (res && res.status === "ok") {
    inputUrl.value = "";
    showToast(res.message || "Đã thêm vào hàng đợi!");
    btnCancel.classList.remove("hidden");
    isPipelineRunning = true;
    toggleTracker(true);
    loadJobs();
    pollProgress();
  } else {
    showErrorModal(res?.error || "Không thể khởi chạy pipeline");
  }
}

function toggleTracker(expand) {
  if (!trackerPanel) return;
  if (expand === undefined) {
    trackerPanel.classList.toggle("tracker-collapsed");
  } else if (expand) {
    trackerPanel.classList.remove("tracker-collapsed");
  } else {
    trackerPanel.classList.add("tracker-collapsed");
  }
}

function fmtElapsed(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function renderLogs(logs) {
  if (!logOutput || !Array.isArray(logs) || !logs.length) return;
  const atBottom = logOutput.scrollTop + logOutput.clientHeight >= logOutput.scrollHeight - 20;
  logOutput.textContent = logs.join("\n");
  if (atBottom) logOutput.scrollTop = logOutput.scrollHeight;
}

function pollProgress() {
  if (progressTimer) clearInterval(progressTimer);
  const startedAt = Date.now();
  let pollTick = 0;
  if (trackerElapsed) trackerElapsed.textContent = "00:00";
  progressTimer = setInterval(async () => {
    pollTick++;
    if (trackerElapsed) trackerElapsed.textContent = fmtElapsed((Date.now() - startedAt) / 1000);
    const res = await apiFetch("/progress");
    if (!res || !res.data) return;
    const d = res.data;
    const pct = Math.round((d.progress || 0) * 100);
    progressFill.style.width = `${pct}%`;
    trackerPct.textContent = `${pct}%`;
    trackerStatus.textContent = d.step_name || "Running...";
    updateStepNodes(d.step_index);
    renderLogs(d.logs);

    // Định kỳ 2 giây (4 ticks) cập nhật lại danh sách job để hiển thị tiêu đề vừa dịch xong
    if (pollTick % 4 === 0) {
      loadJobs();
    }

    if (d.running) {
      isPipelineRunning = true;
      btnCancel.classList.remove("hidden");
    } else {
      if (d.pending_count > 0) {
        // Vẫn còn job chờ trong hàng đợi -> tiếp tục poll cho job kế tiếp
        trackerStatus.textContent = "Đang lấy job tiếp theo...";
        loadJobs();
      } else {
        // Hết sạch hàng đợi
        clearInterval(progressTimer);
        btnCancel.classList.add("hidden");
        isPipelineRunning = false;
        if (d.error) {
          trackerStatus.textContent = "Thất bại";
          showErrorModal(`Pipeline thất bại: ${d.error}`);
        } else {
          trackerStatus.textContent = "Hoàn tất tất cả";
        }
        loadJobs();
        refreshOutputAfterDone();
      }
    }
  }, 500);
}

function updateStepNodes(currentStep) {
  document.querySelectorAll(".step-node").forEach((node) => {
    const step = parseInt(node.getAttribute("data-step"));
    node.classList.remove("active", "done");
    if (step === currentStep) node.classList.add("active");
    else if (step < currentStep) node.classList.add("done");
  });
}

async function cancelPipeline() {
  await apiFetch("/jobs/cancel", { method: "POST" });
  btnCancel.classList.add("hidden"); btnStart.disabled = false;
  trackerStatus.textContent = "Đã hủy"; loadJobs();
}

// ── Concat ───────────────────────────────────────────────────
function showConcatModal() { if (modalConcat) modalConcat.classList.remove("hidden"); }
function hideConcatModal() { if (modalConcat) modalConcat.classList.add("hidden"); }

async function addConcatFiles(files) {
  const paths = [];
  for (const f of files) { const p = await uploadFile(f); if (p) paths.push(p); }
  addConcatPaths(paths);
}

function addConcatPaths(paths) {
  (paths || []).forEach((p) => {
    if (p && !concatFilesList.some((item) => item.path === p))
      concatFilesList.push({ id: Math.random().toString(36).substr(2, 9), path: p, name: p.split(/[\\/]/).pop() });
  });
  renderConcatFileList();
}

function moveConcatItem(index, direction) {
  const t = index + direction;
  if (t >= 0 && t < concatFilesList.length) {
    [concatFilesList[index], concatFilesList[t]] = [concatFilesList[t], concatFilesList[index]];
    renderConcatFileList();
  }
}

function removeConcatItem(index) { concatFilesList.splice(index, 1); renderConcatFileList(); }

function renderConcatFileList() {
  if (!concatFilesListEl) return;
  concatFilesListEl.innerHTML = "";
  concatCountEl.textContent = concatFilesList.length;
  if (concatFilesList.length === 0) {
    concatFilesListEl.innerHTML = '<div class="empty-list-placeholder">Chưa có video. Chọn hoặc kéo thả vào khung trên.</div>';
    btnStartConcat.disabled = true; concatStatusMsg.textContent = "Cần chọn ít nhất 2 video"; return;
  }
  btnStartConcat.disabled = concatFilesList.length < 2;
  concatStatusMsg.textContent = concatFilesList.length >= 2 ? `Sẵn sàng ghép ${concatFilesList.length} video` : "Cần thêm 1 video nữa";

  concatFilesList.forEach((item, i) => {
    const card = document.createElement("div");
    card.className = "concat-item-card";
    card.draggable = true;
    card.dataset.index = i;

    // Drag and Drop reordering
    card.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", i);
      e.dataTransfer.effectAllowed = "move";
      card.classList.add("dragging");
    });

    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      document.querySelectorAll(".concat-item-card").forEach(c => c.classList.remove("drag-over"));
    });

    card.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      card.classList.add("drag-over");
    });

    card.addEventListener("dragleave", () => {
      card.classList.remove("drag-over");
    });

    card.addEventListener("drop", (e) => {
      e.preventDefault();
      card.classList.remove("drag-over");
      const fromIdx = parseInt(e.dataTransfer.getData("text/plain"), 10);
      const toIdx = i;
      if (!isNaN(fromIdx) && fromIdx !== toIdx) {
        const [movedItem] = concatFilesList.splice(fromIdx, 1);
        concatFilesList.splice(toIdx, 0, movedItem);
        renderConcatFileList();
      }
    });

    const left = document.createElement("div"); left.className = "concat-item-left";
    const dragHandle = document.createElement("span"); dragHandle.className = "concat-drag-handle"; dragHandle.textContent = "☰"; dragHandle.title = "Kéo để đổi thứ tự";
    const badge = document.createElement("span"); badge.className = "concat-order-badge"; badge.textContent = `#${i + 1}`;
    const thumb = document.createElement("video"); thumb.className = "concat-thumb-mini"; thumb.preload = "metadata";
    thumb.src = `/media?path=${encodeURIComponent(item.path)}#t=0.5`;
    const info = document.createElement("div"); info.className = "concat-item-info";
    const name = document.createElement("span"); name.className = "concat-item-name"; name.title = item.name; name.textContent = item.name;
    const path = document.createElement("span"); path.className = "concat-item-path"; path.title = item.path; path.textContent = item.path;
    info.appendChild(name); info.appendChild(path);
    left.append(dragHandle, badge, thumb, info);

    const actions = document.createElement("div"); actions.className = "concat-item-actions";
    const up = document.createElement("button"); up.className = "btn-icon-step"; up.textContent = "⬆"; up.disabled = i === 0;
    up.addEventListener("click", () => moveConcatItem(i, -1));
    const down = document.createElement("button"); down.className = "btn-icon-step"; down.textContent = "⬇"; down.disabled = i === concatFilesList.length - 1;
    down.addEventListener("click", () => moveConcatItem(i, 1));
    const del = document.createElement("button"); del.className = "btn-icon-delete"; del.textContent = "🗑️";
    del.addEventListener("click", () => removeConcatItem(i));
    actions.append(up, down, del);
    card.append(left, actions);
    concatFilesListEl.appendChild(card);
  });
}

async function startConcatProcess() {
  if (concatFilesList.length < 2) return;
  btnStartConcat.disabled = true; concatStatusMsg.textContent = "⏳ Đang ghép...";
  showLoading("Đang ghép video...");
  const res = await apiFetch("/tools/concat", { method: "POST", body: JSON.stringify({ files: concatFilesList.map(f => f.path) }) });
  btnStartConcat.disabled = false; hideLoading();
  if (res && res.status === "ok") {
    showToast("Ghép video thành công!", "success");
    hideConcatModal(); concatFilesList = []; renderConcatFileList();
  } else {
    concatStatusMsg.textContent = "❌ Ghép thất bại";
    showToast("Lỗi: " + (res?.error || "Không kết nối server"), "error");
  }
}

// ── Helpers ──────────────────────────────────────────────────
function isUnsupportedPlatform(url) {
  const l = url.toLowerCase();
  return l.includes("youtube.com") || l.includes("youtu.be") || l.includes("facebook.com") || l.includes("fb.watch") || l.includes("fb.com");
}

function clipboardReadWithTimeout() {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    try { navigator.clipboard.readText().then(finish).catch(() => finish(null)); setTimeout(() => finish(null), 1200); } catch (e) { finish(null); }
  });
}

async function pasteClipboard() {
  let text = await clipboardReadWithTimeout();
  if (text != null) { handlePastedText(text); return; }
  try {
    const ta = document.createElement("textarea");
    ta.style.cssText = "position:fixed;top:0;left:0;width:2px;height:2px;opacity:0;";
    document.body.appendChild(ta); ta.focus(); document.execCommand("paste"); text = ta.value; ta.remove();
    if (text) { handlePastedText(text); return; }
  } catch (e2) {}
  showErrorModal("Không thể đọc clipboard. Dán bằng Ctrl+V.");
}

function handlePastedText(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  if (isUnsupportedPlatform(trimmed)) { showErrorModal("Không hỗ trợ link YouTube/Facebook."); return; }
  inputUrl.value = trimmed;
}

// ── Init ─────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  loadJobs(); loadSettings();

  // Polling nhẹ mỗi 3s khi idle để cập nhật tiêu đề dịch nền hoặc trạng thái hàng đợi
  setInterval(() => {
    if (!isPipelineRunning) {
      loadJobs();
    }
  }, 3000);

  btnRefresh.addEventListener("click", loadJobs);
  btnPaste.addEventListener("click", pasteClipboard);
  btnStart.addEventListener("click", startPipeline);
  btnCancel.addEventListener("click", cancelPipeline);

  trackerHeaderToggle?.addEventListener("click", (e) => {
    if (e.target.id === "btn-cancel") return;
    toggleTracker();
  });

  btnFetchTitle?.addEventListener("click", fetchAndTranslateTitle);
  btnChooseOutputDir?.addEventListener("click", chooseOutputDir);
  inputOutputDir?.addEventListener("change", async () => {
    const val = inputOutputDir.value.trim();
    if (val) {
      currentSettings.output_dir = val;
      outputFolderPath = val;
      await apiFetch("/settings", { method: "POST", body: JSON.stringify({ output_dir: val }) });
    }
  });
  btnOpenFolderQuick?.addEventListener("click", () => {
    const folder = inputOutputDir?.value || outputFolderPath || "";
    fetch("/api/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_path: folder }),
    });
  });

  if (btnOpenOutputFolder) {
    btnOpenOutputFolder.addEventListener("click", () => {
      fetch("/api/open-folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder_path: outputFolderPath || "" }) });
    });
  }


  // Error modal close
  document.getElementById("btn-close-modal")?.addEventListener("click", hideErrorModal);
  document.getElementById("btn-error-ok")?.addEventListener("click", hideErrorModal);
  document.getElementById("modal-error")?.addEventListener("click", (e) => { if (e.target.id === "modal-error") hideErrorModal(); });

  // Concat modal
  btnOpenConcat?.addEventListener("click", showConcatModal);
  btnCloseConcatModal?.addEventListener("click", hideConcatModal);
  modalConcat?.addEventListener("click", (e) => { if (e.target === modalConcat) hideConcatModal(); });

  btnConcatSelected?.addEventListener("click", async () => {
    if (selectedJobIds.size === 0) return;
    const paths = [];
    for (const id of selectedJobIds) {
      const res = await apiFetch(`/jobs/output?id=${id}`);
      if (res && res.data && res.data.output_path) {
        paths.push(res.data.output_path);
      }
    }
    if (paths.length === 0) {
      showToast("Không tìm thấy file video kết quả của các job đã chọn", "error");
      return;
    }
    concatFilesList = [];
    addConcatPaths(paths);
    showConcatModal();
  });
  btnBrowseConcatFiles?.addEventListener("click", async () => {
    const api = nativeApi();
    if (api && api.pick_concat_files) { const p = await api.pick_concat_files(); if (p && p.length) addConcatPaths(p); return; }
    if (inputConcatFiles) inputConcatFiles.click();
  });
  inputConcatFiles?.addEventListener("change", (e) => { addConcatFiles(Array.from(e.target.files)); inputConcatFiles.value = ""; });
  if (concatDropzone) {
    concatDropzone.addEventListener("dragover", (e) => { e.preventDefault(); concatDropzone.classList.add("dragover"); });
    concatDropzone.addEventListener("dragleave", () => concatDropzone.classList.remove("dragover"));
    concatDropzone.addEventListener("drop", (e) => { e.preventDefault(); concatDropzone.classList.remove("dragover"); if (e.dataTransfer.files.length) addConcatFiles(Array.from(e.dataTransfer.files)); });
  }
  btnStartConcat?.addEventListener("click", startConcatProcess);

  // Output dir manual input change
  inputOutputDir?.addEventListener("change", async () => {
    const val = inputOutputDir.value.trim();
    if (val) {
      currentSettings.output_dir = val;
      outputFolderPath = val;
      await apiFetch("/settings", { method: "POST", body: JSON.stringify({ output_dir: val }) });
    }
  });

  // Settings modal — Tabs navigation
  const tabBtnBili = document.getElementById("tab-btn-bili");
  const tabBtnTiktok = document.getElementById("tab-btn-tiktok");
  const tabContentBili = document.getElementById("tab-content-bili");
  const tabContentTiktok = document.getElementById("tab-content-tiktok");

  tabBtnBili?.addEventListener("click", () => {
    tabBtnBili.classList.add("active");
    tabBtnTiktok?.classList.remove("active");
    tabContentBili?.classList.add("active");
    tabContentTiktok?.classList.remove("active");
  });

  tabBtnTiktok?.addEventListener("click", () => {
    tabBtnTiktok.classList.add("active");
    tabBtnBili?.classList.remove("active");
    tabContentTiktok?.classList.add("active");
    tabContentBili?.classList.remove("active");
    loadTiktokAccounts();
  });

  // Settings modal — Bilibili login
  btnOpenSettings?.addEventListener("click", () => { modalSettings.classList.remove("hidden"); checkBilibiliStatus(); loadTiktokAccounts(); });
  btnCloseSettings?.addEventListener("click", () => modalSettings.classList.add("hidden"));
  modalSettings?.addEventListener("click", (e) => { if (e.target === modalSettings) modalSettings.classList.add("hidden"); });

  // Bilibili QR + Cookie
  document.getElementById("btn-bili-create-qr")?.addEventListener("click", createBiliQR);
  document.getElementById("btn-bili-save-cookie")?.addEventListener("click", saveBiliCookie);
  document.getElementById("btn-bili-logout")?.addEventListener("click", logoutBili);

  // TikTok TTS accounts
  document.getElementById("btn-tiktok-refresh")?.addEventListener("click", loadTiktokAccounts);
  document.getElementById("btn-tiktok-login")?.addEventListener("click", async () => {
    const res = await apiFetch("/tiktok/open-browser", { method: "POST", body: JSON.stringify({}) });
    showToast(res?.message || "Đã mở trình duyệt");
    let n = 0;
    const timer = setInterval(async () => { await loadTiktokAccounts(); if (++n > 60) clearInterval(timer); }, 5000);
  });
  document.getElementById("btn-tiktok-save-session")?.addEventListener("click", async () => {
    const inp = document.getElementById("input-tiktok-sessionid");
    const val = (inp?.value || "").trim();
    if (!val) { showToast("Vui lòng dán Session ID", "error"); return; }
    const res = await apiFetch("/tiktok/sync-cookie", { method: "POST", body: JSON.stringify({ sessionid: val }) });
    if (res?.status === "ok") {
      showToast("Đã thêm tài khoản TikTok!");
      if (inp) inp.value = "";
      loadTiktokAccounts();
    } else {
      showToast(res?.error || "Lưu thất bại", "error");
    }
  });

  // Voice select change
  selectVoice?.addEventListener("change", async () => {
    currentSettings.voice = selectVoice.value;
    await apiFetch("/settings", { method: "POST", body: JSON.stringify({ voice: selectVoice.value }) });
  });

  // Subtitle style change
  selectSubStyle?.addEventListener("change", async () => {
    currentSettings.sub_style = selectSubStyle.value;
    await apiFetch("/settings", { method: "POST", body: JSON.stringify({ sub_style: selectSubStyle.value }) });
  });

  // Aspect ratio change
  selectAspectRatio?.addEventListener("change", async () => {
    currentSettings.aspect_ratio = selectAspectRatio.value;
    await apiFetch("/settings", { method: "POST", body: JSON.stringify({ aspect_ratio: selectAspectRatio.value }) });
  });

  // Speed slider
  if (speedSlider) {
    speedSlider.addEventListener("input", () => { speedValue.textContent = parseFloat(speedSlider.value).toFixed(2) + "x"; });
    speedSlider.addEventListener("change", async () => {
      currentSettings.video_speed = parseFloat(speedSlider.value);
      await apiFetch("/settings", { method: "POST", body: JSON.stringify({ video_speed: currentSettings.video_speed }) });
    });
  }

  // Volume slider
  if (volumeSlider) {
    volumeSlider.addEventListener("input", () => {
      if (volumeValue) volumeValue.textContent = parseInt(volumeSlider.value, 10) + "%";
    });
    volumeSlider.addEventListener("change", async () => {
      currentSettings.original_volume = parseInt(volumeSlider.value, 10);
      await apiFetch("/settings", { method: "POST", body: JSON.stringify({ original_volume: currentSettings.original_volume }) });
    });
  }

  // Logo — upload lên server rồi giữ đường dẫn tuyệt đối để pipeline đọc được
  btnBrowseLogo?.addEventListener("click", () => inputLogoFile?.click());
  inputLogoFile?.addEventListener("change", async () => {
    const file = inputLogoFile.files && inputLogoFile.files[0];
    if (!file) return;
    const path = await uploadFile(file);
    if (!path) { showErrorModal("Tải logo lên thất bại."); return; }
    inputLogoPath.value = path;
    currentSettings.logo_path = path;
    await apiFetch("/settings", { method: "POST", body: JSON.stringify({ logo_path: path }) });
    showToast("Đã chọn logo");
  });
  selectLogoPos?.addEventListener("change", async () => {
    currentSettings.logo_position = selectLogoPos.value;
    await apiFetch("/settings", { method: "POST", body: JSON.stringify({ logo_position: selectLogoPos.value }) });
  });
  selectLogoOpacity?.addEventListener("change", async () => {
    currentSettings.logo_opacity = parseFloat(selectLogoOpacity.value);
    await apiFetch("/settings", { method: "POST", body: JSON.stringify({ logo_opacity: currentSettings.logo_opacity }) });
  });

  // URL input events — chỉ kiểm tra format, không tự động fetch metadata khi chưa nhấn Run
  inputUrl?.addEventListener("paste", () => {
    setTimeout(() => {
      handlePastedText(inputUrl.value);
    }, 100);
  });
  inputUrl?.addEventListener("input", () => {
    const url = inputUrl.value.trim();
    if (url && isUnsupportedPlatform(url)) showErrorModal("Không hỗ trợ link YouTube/Facebook.");
  });

  // Keyboard shortcuts
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".modal-overlay:not(.hidden)").forEach(m => m.classList.add("hidden"));
    if (e.ctrlKey && e.key === "Enter" && !btnStart.disabled) startPipeline();
  });

  // Clear jobs
  document.getElementById("btn-clear-jobs")?.addEventListener("click", async () => {
    if (!(await showConfirm("Xóa toàn bộ jobs?"))) return;
    const res = await apiFetch("/jobs");
    if (res && res.data) await Promise.all(res.data.map(j => apiFetch(`/jobs?id=${j.id}`, { method: "DELETE" })));
    activeJobId = ""; clearOutput(); loadJobs();
  });

  setInterval(loadJobs, 5000);
});
