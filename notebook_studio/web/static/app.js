(function () {
  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));
  const isLocalHost = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  const hostedFrontend = !isLocalHost;
  const CACHE_KEY = "notebook-studio:dashboard-cache";
  const PREFS_KEY = "notebook-studio:preferences";
  const HELPER_KEY = "notebook-studio:local-helper";
  function readLocal(key, fallback) {
    try { const value = localStorage.getItem(key); return value ? JSON.parse(value) : fallback; }
    catch (_) { return fallback; }
  }
  function writeLocal(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
  }
  const preferences = readLocal(PREFS_KEY, {});
  let state = null;
  let selectedGpu = preferences.gpu || null;
  let controlsReady = false;
  let toastTimer;
  let apiConnected = !hostedFrontend;
  let apiBase = hostedFrontend ? "http://127.0.0.1:8000" : "";
  let helperAuthorization = "";

  function apiUrl(path) { return apiBase + path; }
  async function api(path, options) {
    const requestOptions = Object.assign({}, options || {});
    const headers = new Headers(requestOptions.headers || {});
    if (hostedFrontend && helperAuthorization) headers.set("Authorization", helperAuthorization);
    requestOptions.headers = headers;
    requestOptions.credentials = hostedFrontend ? "omit" : "same-origin";
    const response = await fetch(apiUrl(path), requestOptions);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "Request failed (" + response.status + ")");
    return data;
  }

  function rememberDashboard(data) {
    const cached = JSON.parse(JSON.stringify(data));
    if (cached.budget) cached.budget.active_session = null;
    cached.sessions = (cached.sessions || []).map((session) => {
      const safe = Object.assign({}, session);
      delete safe.sandbox_id;
      delete safe.notebook_url;
      delete safe.notebook_token;
      return safe;
    });
    writeLocal(CACHE_KEY, cached);
  }
  function rememberPreferences() {
    writeLocal(PREFS_KEY, {
      gpu: selectedGpu,
      cpu: Number($("#cpu-select").value || 4),
      ram: Number($("#ram-select").value || 32),
      hours: Number($("#hours-select").value || 2),
      idle: Number($("#idle-select").value || 15),
    });
  }

  function money(value) { return "$" + Number(value || 0).toFixed(2); }
  function bytes(value) {
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
    if (value < 1024 ** 3) return (value / 1024 ** 2).toFixed(1) + " MB";
    return (value / 1024 ** 3).toFixed(2) + " GB";
  }
  function prettyDate(value) {
    if (!value) return "—";
    return new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function showToast(message, error) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.toggle("error", !!error);
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 3500);
  }
  function setMessage(selector, message, error) {
    const el = $(selector);
    el.textContent = message || "";
    el.style.color = error ? "#f0a0a0" : "";
  }

  function navigate(view) {
    const target = view || "overview";
    $$(".view").forEach((section) => section.classList.toggle("active", section.id === "view-" + target));
    $$(".nav-link").forEach((link) => link.classList.toggle("active", link.dataset.view === target));
    const names = { overview: "Overview", notebooks: "Notebooks", datasets: "Datasets", storage: "Storage", usage: "Usage & limits", account: "Account" };
    $("#crumb-current").textContent = names[target] || "Overview";
    if (location.hash !== "#" + target) history.replaceState(null, "", "#" + target);
  }
  $$("[data-view]").forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    navigate(link.dataset.view);
  }));
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));

  function renderBudget(budget) {
    const total = budget.spent_usd + budget.reserved_usd;
    const percentage = budget.app_limit_usd ? Math.min(100, total / budget.app_limit_usd * 100) : 0;
    $("#budget-used").textContent = money(budget.spent_usd);
    $("#budget-limit").textContent = money(budget.app_limit_usd);
    $("#budget-reserved").textContent = money(budget.reserved_usd) + " reserved";
    $("#budget-available").textContent = money(budget.available_usd) + " available";
    $("#budget-meter").style.width = percentage + "%";
    $("#usage-used").textContent = money(budget.spent_usd);
    $("#usage-limit").textContent = money(budget.app_limit_usd);
    $("#usage-reserved").textContent = money(budget.reserved_usd) + " reserved";
    $("#usage-available").textContent = money(budget.available_usd) + " available";
    $("#usage-meter").style.width = percentage + "%";
    $("#usage-month").textContent = budget.month;
    $("#usage-notice").textContent = "Estimate for sessions launched here only. Other Modal apps and provider billing can change your real balance.";
    $("#budget-headline").textContent = budget.reserved_usd > 0 ? "A session is using reserved budget" : (budget.available_usd > 0 ? "Budget guard is ready" : "App budget is fully allocated");
    $("#modal-state").textContent = budget.modal_enabled ? "Modal connected" : "Modal not connected";
    $(".provider-badge").classList.toggle("enabled", budget.modal_enabled);
    $("#account-connection-title").textContent = budget.modal_enabled ? "Modal account connected" : "Not connected";
    $("#account-connection-copy").textContent = budget.modal_enabled ? "Your compute and persistent Volume are created under the connected Modal workspace." : "Connect your own Modal API token. It is encrypted before it is saved and never shown back.";
    $("#account-connection-pill").textContent = budget.modal_enabled ? "CONNECTED" : "DISCONNECTED";
    $("#account-connection-pill").classList.toggle("connected", budget.modal_enabled);
    $("#disconnect-modal-button").classList.toggle("hidden", !state.modal_credentials_saved);
    if (state.username) {
      $("#account-username").textContent = "Signed in as " + state.username;
      $("#account-avatar").textContent = state.username.slice(0, 1).toUpperCase();
    }
    $("#launch-button").disabled = !apiConnected || !budget.modal_enabled || !selectedGpu || budget.available_usd <= 0 || !!budget.active_session;
    $("#launch-button").title = !budget.modal_enabled ? "Enable Modal in .env to launch" : "";
  }

  function resourceValues() {
    const cpu = Number($("#cpu-select").value || 4);
    const ram = Number($("#ram-select").value || 32);
    const hours = Number($("#hours-select").value || 2);
    return { cpu, ram, hours };
  }
  function selectedOption() { return state && state.gpus.find((gpu) => gpu.key === selectedGpu); }
  function computeRate(gpu) {
    if (!gpu || !state) return 0;
    const v = resourceValues();
    return gpu.gpu_usd_per_hour + v.cpu * state.cpu_usd_per_core_hour + v.ram * state.ram_usd_per_gib_hour;
  }
  function updateEstimate() {
    const gpu = selectedOption();
    const button = $("#launch-button");
    if (!gpu) {
      $("#launch-estimate").textContent = "Select a GPU";
      $("#launch-rate").textContent = "Rates include Sandbox CPU and RAM.";
      button.disabled = true;
      return;
    }
    const budget = state.budget;
    const values = resourceValues();
    const capHours = Math.min(values.hours, state.max_session_hours, budget.available_usd / computeRate(gpu));
    const estimate = computeRate(gpu) * capHours;
    $("#launch-estimate").textContent = capHours > 0 ? money(estimate) + " for up to " + capHours.toFixed(1) + "h" : "Over available budget";
    $("#launch-rate").textContent = money(computeRate(gpu)) + "/hour · includes CPU + RAM";
    if (!apiConnected || !budget.modal_enabled || !gpu || budget.available_usd <= 0 || budget.active_session) button.disabled = true;
  }
  function renderGpuOptions() {
    const container = $("#gpu-grid");
    container.innerHTML = state.gpus.map((gpu) => {
      const selected = gpu.key === selectedGpu;
      return '<label class="gpu-option ' + (selected ? "selected" : "") + '" data-gpu="' + escapeHtml(gpu.key) + '"><input type="radio" name="gpu" value="' + escapeHtml(gpu.key) + '" ' + (selected ? "checked" : "") + '><div class="gpu-tile"><div class="gpu-title"><strong>' + escapeHtml(gpu.name.replace("NVIDIA ", "")) + '</strong><span class="gpu-check">' + (selected ? "✓" : "") + '</span></div><div class="gpu-vram">' + gpu.vram_gib + ' GB VRAM</div><div class="gpu-rate">From ' + money(gpu.hourly_rate_4cpu_32gib) + '/hr · 4 CPU / 32 GB</div>' + (gpu.note ? '<div class="gpu-note">' + escapeHtml(gpu.note) + '</div>' : "") + '</div></label>';
    }).join("");
    $$(".gpu-option", container).forEach((label) => label.addEventListener("click", () => {
      selectedGpu = label.dataset.gpu;
      rememberPreferences();
      renderGpuOptions();
      updateEstimate();
      renderRateTable();
    }));
  }
  function fillSelects() {
    $("#cpu-select").innerHTML = state.cpu_choices.map((v) => '<option value="' + v + '" ' + (v === (preferences.cpu || 4) ? "selected" : "") + '>' + v + ' cores</option>').join("");
    $("#ram-select").innerHTML = state.ram_choices_gib.map((v) => '<option value="' + v + '" ' + (v === (preferences.ram || 32) ? "selected" : "") + '>' + v + ' GiB</option>').join("");
    const max = Math.max(0.1, state.max_session_hours);
    const steps = [0.5, 1, 2, 4, 6, 8, 12, 24].filter((v) => v <= max);
    if (!steps.includes(max)) steps.push(max);
    $("#hours-select").innerHTML = steps.map((v) => '<option value="' + v + '" ' + (v === (preferences.hours || Math.min(2, max)) ? "selected" : "") + '>' + v + (v === 1 ? " hour" : " hours") + '</option>').join("");
    const defaults = [5, 10, 15, 30, 60];
    $("#idle-select").innerHTML = defaults.map((v) => '<option value="' + v + '" ' + (v === (preferences.idle || state.default_idle_timeout_minutes) ? "selected" : "") + '>' + v + ' min</option>').join("");
    ["#cpu-select", "#ram-select", "#hours-select", "#idle-select"].forEach((selector) => {
      $(selector).onchange = () => { rememberPreferences(); updateEstimate(); };
    });
  }
  function renderRateTable() {
    if (!state) return;
    $("#rate-list").innerHTML = state.gpus.map((gpu) => '<tr><td><strong>' + escapeHtml(gpu.name) + '</strong><small>' + escapeHtml(gpu.note || "Single GPU") + '</small></td><td>' + gpu.vram_gib + ' GB</td><td>' + money(gpu.gpu_usd_per_hour) + '</td><td>' + money(gpu.hourly_rate_4cpu_32gib) + '</td><td><button class="choose-rate" data-choose="' + escapeHtml(gpu.key) + '">Choose</button></td></tr>').join("");
    $$("[data-choose]").forEach((button) => button.addEventListener("click", () => {
      selectedGpu = button.dataset.choose;
      rememberPreferences();
      renderGpuOptions();
      updateEstimate();
      navigate("overview");
      window.scrollTo({ top: 0, behavior: "smooth" });
    }));
  }
  function renderSession(session) {
    const card = $("#active-session-card");
    const status = $("#session-status");
    const stop = $("#stop-session");
    const open = $("#open-notebook");
    const content = $("#active-session-content");
    const current = session && ["launching", "running", "stopping"].includes(session.status);
    if (!current) {
      $("#session-heading").textContent = "No session running";
      status.textContent = "IDLE";
      status.className = "session-status idle";
      content.className = "empty-session";
      content.innerHTML = '<span class="empty-mark">⌘</span><div><strong>Pick a GPU to get started</strong><span>Your files will stay in persistent storage.</span></div>';
      stop.classList.add("hidden");
      open.disabled = true;
      open.onclick = null;
    } else {
      $("#session-heading").textContent = session.status === "launching" ? "Starting JupyterLab" : "JupyterLab is ready";
      status.textContent = session.status.toUpperCase();
      status.className = "session-status running";
      content.className = "empty-session active-session-detail";
      content.innerHTML = '<span class="empty-mark">' + escapeHtml(session.gpu_key) + '</span><div><strong>' + escapeHtml(session.gpu_key.replace("-", " ")) + ' · ' + session.cpus + ' CPU · ' + session.memory_gib + ' GiB</strong><span>Up to ' + (session.max_runtime_seconds / 3600).toFixed(1) + ' hours · reserved ' + money(session.reserved_usd) + '</span></div>';
      stop.classList.remove("hidden");
      stop.disabled = false;
      stop.textContent = "Stop session";
      open.disabled = !session.notebook_url;
      open.onclick = () => window.open(session.notebook_url, "_blank", "noopener,noreferrer");
      stop.onclick = () => stopSession(session.id);
    }
    renderNotebookPage(session, current);
    if (card) card.dataset.session = current ? session.id : "";
  }
  function renderNotebookPage(session, current) {
    const status = $("#notebook-current-status");
    if (!current) {
      $("#notebook-current-title").textContent = "No active notebook";
      status.textContent = "IDLE";
      status.className = "session-status idle";
      $("#notebook-current-body").className = "empty-notebook";
      $("#notebook-current-body").textContent = "Launch a session to see its connection and stop controls here.";
      return;
    }
    $("#notebook-current-title").textContent = session.status === "launching" ? "Starting JupyterLab" : "JupyterLab is ready";
    status.textContent = session.status.toUpperCase();
    status.className = "session-status running";
    const body = $("#notebook-current-body");
    body.className = "empty-notebook";
    body.innerHTML = '<div>' + escapeHtml(session.gpu_key) + ' · ' + session.cpus + ' CPU · ' + session.memory_gib + ' GiB RAM · reserved ' + money(session.reserved_usd) + '</div>' + (session.notebook_url ? '<a class="button button-primary notebook-link" href="' + escapeHtml(session.notebook_url) + '" target="_blank" rel="noreferrer">Open JupyterLab ↗</a>' : '<span>Waiting for the Jupyter server to become ready…</span>');
  }
  function renderSessions(sessions) {
    const rows = sessions.slice(0, 8);
    $("#recent-sessions").innerHTML = rows.length ? rows.map((s) => '<tr><td><span class="status-cell"><i class="' + (s.status === "running" ? "running" : "") + '"></i>' + escapeHtml(s.status) + '</span></td><td class="mono">' + escapeHtml(s.gpu_key) + '</td><td>' + s.cpus + ' CPU · ' + s.memory_gib + ' GiB</td><td>' + (s.max_runtime_seconds / 3600).toFixed(1) + 'h</td><td>' + money(s.billed_usd || s.reserved_usd) + '</td></tr>').join("") : '<tr><td colspan="5" class="table-empty">No sessions yet. Your first one will show up here.</td></tr>';
  }
  function renderDatasets(datasets) {
    $("#dataset-count").textContent = datasets.length;
    if (!datasets.length) {
      $("#dataset-list").innerHTML = '<div class="empty-dataset"><span>▧</span><strong>No uploaded datasets yet</strong><small>Files you upload here will persist between sessions.</small></div>';
      return;
    }
    $("#dataset-list").innerHTML = datasets.map((d) => '<div class="dataset-row"><div class="dataset-name"><span class="file-badge">▧</span><div><strong>' + escapeHtml(d.name) + '</strong><small>' + escapeHtml("/workspace" + d.path) + '</small></div></div><span class="dataset-meta">' + bytes(d.size_bytes) + '</span><span class="dataset-meta dataset-date">' + prettyDate(d.uploaded_at) + '</span><button class="delete-button" data-delete="' + escapeHtml(d.id) + '">Delete</button></div>').join("");
    $$("[data-delete]").forEach((button) => button.addEventListener("click", async () => {
      if (!window.confirm("Delete this file from the persistent Volume?")) return;
      button.disabled = true;
      try { await api("/api/datasets/" + encodeURIComponent(button.dataset.delete), { method: "DELETE" }); showToast("Dataset deleted from persistent storage."); await refresh(); }
      catch (error) { button.disabled = false; showToast(error.message, true); }
    }));
  }
  function applyDashboard(data) {
    state = data;
    if (!selectedGpu || !state.gpus.some((item) => item.key === selectedGpu)) selectedGpu = state.gpus.some((item) => item.key === "L4") ? "L4" : state.gpus[0].key;
    const budget = state.budget;
    if (!controlsReady) { fillSelects(); controlsReady = true; }
    renderGpuOptions();
    renderBudget(budget);
    renderSessions(state.sessions);
    renderDatasets(state.datasets);
    renderSession(budget.active_session);
    renderRateTable();
    updateEstimate();
    $("#upload-limit").textContent = bytes(state.upload_limit_bytes);
    if (!budget.modal_enabled) setMessage("#launch-message", "Connect your own Modal API token in Account settings before launching.", false);
    else setMessage("#launch-message", "", false);
  }
  async function refresh() {
    try {
      const data = await api("/api/dashboard");
      apiConnected = true;
      applyDashboard(data);
      rememberDashboard(data);
      if (hostedFrontend) {
        $("#helper-overlay").classList.add("hidden");
        $("#helper-status").textContent = "Local helper connected";
      }
      return true;
    } catch (error) {
      apiConnected = false;
      if (hostedFrontend) {
        $("#helper-overlay").classList.remove("hidden");
        const authFailed = error.message.includes("Authentication required");
        $("#helper-message").textContent = authFailed
          ? "Sign-in failed. Check the local app username and password."
          : "Could not reach the local helper. Start it with ./run.sh, then allow this site to access localhost if your browser asks.";
        const cached = readLocal(CACHE_KEY, null);
        if (cached && cached.gpus && cached.budget) {
          applyDashboard(cached);
          $("#helper-cache-note").textContent = "Showing this browser's saved dashboard snapshot. Connect your local helper to refresh it.";
        }
        updateEstimate();
      } else {
        showToast(error.message, true);
        if (error.message.includes("Authentication required")) $("#modal-state").textContent = "Sign-in required";
      }
      return false;
    }
  }
  async function launch() {
    const gpu = selectedGpu;
    const values = resourceValues();
    const button = $("#launch-button");
    button.disabled = true;
    button.textContent = "Reserving budget…";
    setMessage("#launch-message", "Checking the full runtime estimate, then starting the Sandbox…", false);
    try {
      const rate = computeRate(selectedOption());
      const safeHours = Math.min(values.hours, state.max_session_hours, state.budget.available_usd / rate);
      if (safeHours * 3600 < 60) throw new Error("Available app budget cannot reserve one minute for this GPU.");
      await api("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ gpu, cpus: values.cpu, memory_gib: values.ram, max_hours: safeHours, idle_timeout_minutes: Number($("#idle-select").value) })
      });
      showToast("Notebook is ready. Open JupyterLab from the session card.");
      setMessage("#launch-message", "", false);
      await refresh();
    } catch (error) {
      setMessage("#launch-message", error.message, true);
      showToast(error.message, true);
      await refresh();
    } finally {
      button.innerHTML = 'Launch notebook <span>→</span>';
      updateEstimate();
    }
  }
  async function stopSession(sessionId) {
    const button = $("#stop-session");
    button.disabled = true;
    button.textContent = "Stopping…";
    try {
      await api("/api/sessions/" + encodeURIComponent(sessionId) + "/stop", { method: "POST" });
      showToast("Session stopped. Final app estimate has been recorded.");
      await refresh();
    } catch (error) {
      showToast(error.message, true);
      button.disabled = false;
      button.textContent = "Stop session";
    }
  }
  function uploadFile(file) {
    if (!file) return;
    if (!apiConnected) { setMessage("#upload-message", "Connect your local helper first.", true); return; }
    const limit = state ? state.upload_limit_bytes : 0;
    if (file.size > limit) { setMessage("#upload-message", "File exceeds the configured upload limit of " + bytes(limit) + ".", true); return; }
    const progress = $(".upload-progress");
    const fill = $("#upload-progress-fill");
    const button = $("#choose-file");
    const form = new FormData();
    form.append("file", file);
    progress.classList.remove("hidden");
    fill.style.width = "0%";
    button.disabled = true;
    setMessage("#upload-message", "Uploading " + file.name + "…", false);
    const request = new XMLHttpRequest();
    request.open("POST", apiUrl("/api/datasets"));
    if (hostedFrontend && helperAuthorization) request.setRequestHeader("Authorization", helperAuthorization);
    request.upload.onprogress = (event) => { if (event.lengthComputable) fill.style.width = Math.round(event.loaded / event.total * 100) + "%"; };
    request.onload = async () => {
      button.disabled = false;
      progress.classList.add("hidden");
      let result = {};
      try { result = JSON.parse(request.responseText); } catch (_) {}
      if (request.status >= 200 && request.status < 300) {
        const note = result.note || "Uploaded " + file.name + " to persistent storage.";
        setMessage("#upload-message", note, false);
        showToast("Dataset uploaded.");
        await refresh();
      } else {
        setMessage("#upload-message", result.detail || "Upload failed.", true);
      }
    };
    request.onerror = () => { button.disabled = false; progress.classList.add("hidden"); setMessage("#upload-message", "Network error during upload.", true); };
    request.send(form);
  }
  $("#launch-button").addEventListener("click", launch);
  $("#modal-credentials-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("#connect-modal-button");
    button.disabled = true;
    button.textContent = "Checking token…";
    setMessage("#account-message", "Verifying this token with Modal, then saving it encrypted…", false);
    try {
      const result = await api("/api/modal-credentials", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token_id: $("#modal-token-id").value.trim(), token_secret: $("#modal-token-secret").value })
      });
      $("#modal-token-id").value = "";
      $("#modal-token-secret").value = "";
      setMessage("#account-message", result.detail, false);
      showToast("Modal workspace connected.");
      await refresh();
    } catch (error) {
      setMessage("#account-message", error.message, true);
      showToast(error.message, true);
    } finally {
      button.innerHTML = 'Connect Modal <span>→</span>';
      button.disabled = false;
    }
  });
  $("#disconnect-modal-button").addEventListener("click", async () => {
    if (!window.confirm("Forget your saved token? Revoke it separately in Modal if you want it disabled.")) return;
    try {
      const result = await api("/api/modal-credentials", { method: "DELETE" });
      setMessage("#account-message", result.detail, false);
      await refresh();
    } catch (error) { setMessage("#account-message", error.message, true); }
  });
  $("#refresh-button").addEventListener("click", refresh);
  $("#refresh-datasets").addEventListener("click", refresh);
  function openHelperDialog() {
    const saved = readLocal(HELPER_KEY, {});
    $("#helper-url").value = saved.url || "http://127.0.0.1:8000";
    $("#helper-username").value = saved.username || "local";
    $("#helper-password").value = "";
    $("#helper-message").textContent = "";
    $("#helper-overlay").classList.remove("hidden");
    $("#helper-password").focus();
  }
  $("#connect-local-button").addEventListener("click", openHelperDialog);
  $("#helper-connect").addEventListener("click", async () => {
    const rawUrl = $("#helper-url").value.trim();
    const username = $("#helper-username").value.trim();
    const password = $("#helper-password").value;
    let parsed;
    try { parsed = new URL(rawUrl); } catch (_) { $("#helper-message").textContent = "Enter a valid local helper URL."; return; }
    if (!["http:", "https:"].includes(parsed.protocol) || !["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname)) {
      $("#helper-message").textContent = "For safety, the hosted page only connects to localhost on this device.";
      return;
    }
    if (!username || !password) { $("#helper-message").textContent = "Enter the local app username and password."; return; }
    apiBase = parsed.origin;
    helperAuthorization = "Basic " + btoa(username + ":" + password);
    const button = $("#helper-connect");
    button.disabled = true;
    button.textContent = "Connecting…";
    $("#helper-message").textContent = "Checking the local app…";
    const connected = await refresh();
    if (connected) {
      writeLocal(HELPER_KEY, { url: parsed.origin, username });
      $("#helper-password").value = "";
      $("#helper-cache-note").textContent = "Dashboard preferences and a safe metadata snapshot are stored in this browser. Modal files remain in your own Volume.";
    } else {
      helperAuthorization = "";
    }
    button.disabled = false;
    button.textContent = "Connect local helper";
  });
  $("#choose-file").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", (event) => uploadFile(event.target.files[0]));
  const drop = $("#drop-zone");
  ["dragenter", "dragover"].forEach((type) => drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((type) => drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.remove("dragover"); }));
  drop.addEventListener("drop", (event) => uploadFile(event.dataTransfer.files[0]));
  navigate(location.hash.slice(1) || "overview");
  if (hostedFrontend) {
    const cached = readLocal(CACHE_KEY, null);
    if (cached && cached.gpus && cached.budget) applyDashboard(cached);
    $("#helper-status").textContent = "Local helper disconnected";
    $("#connect-local-button").classList.remove("hidden");
    $("#helper-overlay").classList.remove("hidden");
    const saved = readLocal(HELPER_KEY, {});
    $("#helper-url").value = saved.url || "http://127.0.0.1:8000";
    $("#helper-username").value = saved.username || "local";
    $("#helper-cache-note").textContent = cached ? "Saved dashboard data is local to this browser." : "No dashboard data is saved in this browser yet.";
  } else {
    refresh();
  }
  setInterval(() => {
    if (document.visibilityState === "visible" && state && state.budget.active_session) refresh();
  }, 60000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state && state.budget.active_session) refresh();
  });
}());
