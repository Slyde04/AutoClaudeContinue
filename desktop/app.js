/* global window, document, EventSource, fetch, URLSearchParams, setTimeout, clearTimeout, setInterval */

(() => {
  "use strict";

  const TAURI = window.__TAURI__;
  const invoke = TAURI && TAURI.core ? TAURI.core.invoke : null;

  const STATE_LABELS = {
    starting: "Starting",
    disabled: "Detection paused",
    waiting: "Waiting for reset",
    sending: "Sending now",
    sent: "Message sent",
    idle: "Monitoring",
    not_found: "App not found",
    not_ready: "Not readable",
    error: "Needs attention",
    deferred: "Waiting for you",
    stopped: "Stopped",
  };

  const STATE_TERMINAL = new Set([
    "sent",
    "error",
    "not_found",
    "deferred",
    "waiting",
    "stopped",
    "idle",
  ]);

  const PAGE_META = {
    home: [
      "Home",
      "Watch for a rate limit, wait it out, then send your message.",
    ],
    settings: ["Settings", "Everything the app does, and when it does it."],
    misc: ["Misc", "About the app, its creator, and the license."],
  };

  const APP_LABELS = { claude: "Claude", chatgpt: "ChatGPT", auto: "Automatic" };
  const UI_SCALES = [90, 100, 110, 125];

  const ui = {
    apiBase: "",
    token: "",
    version: "1.2.0",
    config: null,
    state: null,
    activity: [],
    chats: [],
    chatAppLabel: "",
    page: "home",
    events: null,
    bootstrapDone: false,
    sendPending: false,
    sendTimer: null,
    messageTimer: null,
    settingsTimer: null,
    toastTimer: null,
    chatsTimer: null,
    startupSynced: false,
    lastAttachments: "",
    lastPresets: "",
    lastChats: "",
    lastMessageSaved: 0,
  };

  const $ = (id) => document.getElementById(id);

  function dotClass(color) {
    return `dot-${["green", "orange", "red", "blue", "gray"].includes(color) ? color : "gray"}`;
  }

  function textClass(color) {
    return `text-${["green", "orange", "red", "blue", "gray"].includes(color) ? color : "gray"}`;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function basename(path) {
    const parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || String(path);
  }

  function truncate(text, length) {
    return text.length <= length ? text : `${text.slice(0, length - 1)}…`;
  }

  /* -------------------------------------------------------------------------
     API
  ------------------------------------------------------------------------- */

  async function resolveApi() {
    if (invoke) {
      try {
        const info = await invoke("api_config");
        if (info && info.port) {
          ui.apiBase = `http://127.0.0.1:${info.port}`;
          ui.token = info.token || "";
          return;
        }
      } catch (error) {
        console.warn("api_config failed", error);
      }
    }
    ui.apiBase = window.location.origin;
    const params = new URLSearchParams(window.location.search);
    ui.token = params.get("token") || "";
  }

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = {};
    if (ui.token) headers["X-ACC-Token"] = ui.token;
    let body;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }
    const response = await fetch(`${ui.apiBase}${path}`, {
      method,
      headers,
      body,
      cache: "no-store",
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const payload = await response.json();
        if (payload && payload.error) detail = payload.error;
      } catch (_error) {
        /* keep the status text */
      }
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return response.json();
  }

  async function bootstrapWithRetry() {
    let lastError = null;
    for (let attempt = 0; attempt < 90; attempt += 1) {
      try {
        return await api("/api/bootstrap");
      } catch (error) {
        lastError = error;
        if (attempt === 0) {
          setStartingState();
        }
        await sleep(350);
      }
    }
    throw lastError || new Error("The automation engine did not answer.");
  }

  function setStartingState() {
    renderState({
      state: "starting",
      title: "Starting AutoClaudeContinue",
      detail: "Waking up the automation engine.",
      color: "gray",
      app_label: "",
      reset_at: null,
      retry_at: null,
    });
  }

  /* -------------------------------------------------------------------------
     Boot
  ------------------------------------------------------------------------- */

  async function init() {
    bindStaticHandlers();
    bindPageNavigation();
    await resolveApi();
    try {
      const snapshot = await bootstrapWithRetry();
      await applySnapshot(snapshot);
      bindTrayEvents();
      connectEvents();
    } catch (error) {
      console.error(error);
      renderState({
        state: "error",
        title: "Backend unavailable",
        detail:
          "The automation engine did not start. Check %APPDATA%\\AutoClaudeContinue\\app.log and relaunch the app.",
        color: "red",
        app_label: "",
        reset_at: null,
        retry_at: null,
      });
      showToast("The automation engine did not start.", true);
    }
    setInterval(tickCountdown, 1000);
  }

  function bindTrayEvents() {
    const eventApi = TAURI && TAURI.event;
    if (!eventApi) return;
    eventApi.listen("tray-send-now", () => sendNow()).catch(() => {});
    eventApi
      .listen("tray-toggle-detection", (event) => {
        setDetection(event.payload === true);
      })
      .catch(() => {});
  }

  async function applySnapshot(snapshot) {
    ui.bootstrapDone = true;
    ui.version = snapshot.version || ui.version;
    ui.config = snapshot.config || {};
    ui.state = snapshot.state || null;
    ui.activity = Array.isArray(snapshot.activity) ? snapshot.activity.slice() : [];
    ui.chats = Array.isArray(snapshot.chats) ? snapshot.chats.slice() : [];
    ui.chatAppLabel = snapshot.chat_app_label || "";

    $("version-label").textContent = `v${ui.version}`;
    $("license-subtitle").textContent = `AutoClaudeContinue · v${ui.version} · KutaOS`;

    applyTheme(ui.config.theme || "dark", false);
    applyScale(ui.config.ui_scale || 100, false);
    renderConfig(ui.config);
    renderState(ui.state);
    renderActivity();
    renderAttachments(true);
    renderPresets(true);
    renderChats(true);
    setCloseToTray(ui.config.minimize_to_tray !== false);
    await syncStartup(ui.config);
  }

  function connectEvents() {
    if (ui.events) {
      ui.events.close();
      ui.events = null;
    }
    const query = ui.token ? `?token=${encodeURIComponent(ui.token)}` : "";
    const source = new EventSource(`${ui.apiBase}/api/events${query}`);
    ui.events = source;

    const handle = (kind) => (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        console.warn("Bad event payload", error);
        return;
      }
      handleEvent(kind, payload);
    };

    ["hello", "state", "chats", "draft_reset", "log", "dev_mode", "config"].forEach(
      (kind) => source.addEventListener(kind, handle(kind)),
    );

    source.addEventListener("error", () => {
      if (ui.state && ui.state.state !== "disabled") {
        // EventSource reconnects automatically; don't replace the automation
        // state with a scary error.  The sidebar keeps the last real state.
        console.debug("Event stream reconnecting");
      }
    });
  }

  function handleEvent(kind, payload) {
    if (kind === "hello") {
      const snapshot = payload.snapshot || {};
      if (!snapshot.config) return;
      ui.config = snapshot.config;
      ui.state = snapshot.state || ui.state;
      ui.activity = Array.isArray(snapshot.activity)
        ? snapshot.activity.slice()
        : ui.activity;
      ui.chats = Array.isArray(snapshot.chats) ? snapshot.chats.slice() : ui.chats;
      ui.chatAppLabel = snapshot.chat_app_label || ui.chatAppLabel;
      ui.version = snapshot.version || ui.version;
      $("version-label").textContent = `v${ui.version}`;
      renderState(ui.state);
      renderActivity();
      renderConfig(ui.config);
      renderChats();
      return;
    }

    if (kind === "state") {
      renderState(payload);
      return;
    }

    if (kind === "chats") {
      ui.chats = Array.isArray(payload.chats) ? payload.chats : [];
      ui.chatAppLabel = payload.app_label || "";
      $("refresh-chats").disabled = false;
      $("refresh-chats").textContent = "Refresh";
      renderChats();
      return;
    }

    if (kind === "draft_reset") {
      if (ui.config) {
        ui.config.message = payload.message || "continue";
        ui.config.saved_messages = payload.saved_messages || ui.config.saved_messages;
        ui.config.attachments = payload.attachments || [];
      }
      $("message-box").value = payload.message || "continue";
      $("message-box").dataset.dirty = "0";
      renderPresets(true);
      renderAttachments(true);
      showMessageSaved("Sent message saved as a preset", "green", 2200);
      return;
    }

    if (kind === "log") {
      appendActivity({
        time: timeStamp(),
        message: payload.message || "",
        level: payload.level || "info",
      });
      return;
    }

    if (kind === "dev_mode") {
      if (ui.config) ui.config.claude_dev_mode = Boolean(payload.enabled);
      setSwitch($("dev-mode-switch"), Boolean(payload.enabled));
      appendActivity({
        time: timeStamp(),
        message: "Claude developer mode detected and enabled.",
      });
      showToast("Claude developer mode detected and enabled.");
      return;
    }

    if (kind === "config") {
      const next = payload.config || {};
      const previous = ui.config || {};
      ui.config = next;
      if (previous.theme !== next.theme) {
        applyTheme(next.theme || "dark", false);
      }
      if (Number(previous.ui_scale) !== Number(next.ui_scale)) {
        applyScale(next.ui_scale || 100, false);
      }
      if (Boolean(previous.minimize_to_tray) !== Boolean(next.minimize_to_tray)) {
        setCloseToTray(next.minimize_to_tray !== false);
      }
      renderConfig(next, { preserveFocus: true });
    }
  }

  /* -------------------------------------------------------------------------
     Rendering
  ------------------------------------------------------------------------- */

  function renderConfig(config, options = {}) {
    if (!config) return;
    const preserveFocus = options.preserveFocus !== false;

    document.querySelectorAll("[data-target]").forEach((button) => {
      button.classList.toggle(
        "is-active",
        button.dataset.target === (config.app_target || "claude"),
      );
    });

    setSwitch($("detection-switch"), config.detection_enabled !== false);
    $("detection-label").textContent =
      config.detection_enabled !== false ? "Detection on" : "Detection off";

    setSwitch($("dev-mode-switch"), config.claude_dev_mode !== false);
    setSwitch($("startup-switch"), Boolean(config.start_with_windows));
    setSwitch($("tray-switch"), config.minimize_to_tray !== false);
    setSwitch($("launch-switch"), Boolean(config.launch_if_missing));
    setSwitch($("drafts-switch"), config.preserve_drafts !== false);
    setSwitch($("active-switch"), config.defer_when_active !== false);
    setSwitch($("locked-switch"), config.pause_when_locked !== false);
    setSwitch($("light-switch"), (config.theme || "dark") === "light");

    const messageBox = $("message-box");
    if (
      typeof config.message === "string" &&
      messageBox.dataset.dirty !== "1" &&
      (!preserveFocus || document.activeElement !== messageBox) &&
      messageBox.value !== config.message
    ) {
      messageBox.value = config.message;
    }

    setInputWhenIdle(
      $("interval-input"),
      String(Math.max(1, Math.round((config.poll_seconds || 900) / 60))),
      preserveFocus,
    );
    setInputWhenIdle(
      $("wake-input"),
      String(config.wake_early_seconds ?? 10),
      preserveFocus,
    );
    setInputWhenIdle(
      $("grace-input"),
      String(config.active_grace_seconds ?? 20),
      preserveFocus,
    );

    document.querySelectorAll("[data-scale]").forEach((button) => {
      button.classList.toggle(
        "is-active",
        Number(button.dataset.scale) === Number(config.ui_scale || 100),
      );
    });

    if (invoke) {
      invoke("set_tray_detection", {
        enabled: config.detection_enabled !== false,
      }).catch(() => {});
    }

    renderPresets();
    renderChats();
    renderAttachments();
  }

  function setInputWhenIdle(input, value, preserveFocus) {
    if (!input) return;
    if (preserveFocus && document.activeElement === input) return;
    if (input.value !== value) input.value = value;
  }

  function setSwitch(element, value) {
    if (!element) return;
    const state = value ? "true" : "false";
    element.setAttribute("aria-checked", state);
    const inner = element.querySelector(".switch");
    if (inner) inner.setAttribute("aria-checked", state);
  }

  function isSwitchOn(element) {
    return element.getAttribute("aria-checked") === "true";
  }

  function renderPresets(force = false) {
    if (!ui.config) return;
    const values = ["", ...(ui.config.saved_messages || [])];
    const key = JSON.stringify(values);
    if (!force && key === ui.lastPresets) return;
    ui.lastPresets = key;
    const select = $("preset-select");
    select.innerHTML = "";
    values.forEach((value, index) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = index === 0 ? "Presets" : value;
      select.appendChild(option);
    });
    select.value = "";
  }

  function renderChats(force = false) {
    if (!ui.config) return;
    const current = ui.config.chat_name || "";
    const values = ["", ...ui.chats];
    if (current && !values.includes(current)) values.splice(1, 0, current);
    const key = JSON.stringify([values, current]);
    if (!force && key === ui.lastChats) {
      updateChatHint();
      return;
    }
    ui.lastChats = key;
    const select = $("chat-select");
    select.innerHTML = "";
    values.forEach((value, index) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = index === 0 ? "Current chat" : value;
      select.appendChild(option);
    });
    select.value = current;
    updateChatHint();
  }

  function updateChatHint() {
    const hint = $("chat-hint");
    if (ui.chats.length) {
      hint.textContent = `${ui.chats.length} chats found in the sidebar.`;
    } else if (ui.config && ui.config.chat_name) {
      hint.textContent = `Using "${ui.config.chat_name}" from your settings.`;
    } else if (ui.chatAppLabel) {
      hint.textContent = `${ui.chatAppLabel} is open, but no sidebar chats were readable.`;
    } else {
      hint.textContent =
        "Use the currently open chat, or refresh to list chats.";
    }
  }

  function renderAttachments(force = false) {
    if (!ui.config) return;
    const attachments = ui.config.attachments || [];
    const key = JSON.stringify(attachments);
    if (!force && key === ui.lastAttachments) return;
    ui.lastAttachments = key;

    const list = $("attachment-list");
    const empty = $("images-empty");
    list.innerHTML = "";

    if (!attachments.length) {
      empty.hidden = false;
      list.hidden = true;
      $("attachment-hint").textContent = "Images are optional.";
      $("attachment-hint").className = "muted-note";
      return;
    }

    empty.hidden = true;
    list.hidden = false;
    attachments.forEach((path) => {
      const row = document.createElement("div");
      row.className = "attachment-row";

      const thumb = document.createElement("img");
      thumb.className = "attachment-thumb";
      thumb.alt = "";
      const query = [
        `path=${encodeURIComponent(path)}`,
        ui.token ? `token=${encodeURIComponent(ui.token)}` : "",
      ]
        .filter(Boolean)
        .join("&");
      thumb.src = `${ui.apiBase}/api/attachment-thumb?${query}`;
      thumb.addEventListener("error", () => {
        thumb.style.visibility = "hidden";
      });

      const name = document.createElement("span");
      name.className = "attachment-name";
      name.textContent = truncate(basename(path), 30);
      name.title = path;

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "attachment-remove";
      remove.title = "Remove image";
      remove.textContent = "×";
      remove.addEventListener("click", () => removeAttachment(path));

      row.append(thumb, name, remove);
      list.appendChild(row);
    });

    const count = attachments.length;
    $("attachment-hint").textContent = `${count} image${count === 1 ? "" : "s"} attached.`;
    $("attachment-hint").className = "muted-note";
  }

  function renderState(state) {
    if (!state) return;
    ui.state = state;
    const color = state.color || "gray";
    const label = STATE_LABELS[state.state] || titleCase(state.state || "idle");

    $("status-dot").className = `dot ${dotClass(color)}`;
    $("status-state").textContent = label;
    $("status-state").className = textClass(color);
    $("status-title").textContent = state.title || "";
    $("status-detail").textContent = state.detail || "";
    $("status-app").textContent = state.app_label || "AutoClaudeContinue";

    const sidebarStatus = $("sidebar-status");
    sidebarStatus.textContent = label;
    sidebarStatus.className = textClass(color);
    $("sidebar-dot").className = `dot ${dotClass(color)}`;

    if (state.retry_at) {
      $("status-reset").textContent = "Reset passed — retrying";
    } else if (state.reset_at) {
      $("status-reset").textContent = state.estimated
        ? "Estimated reset time"
        : "Wake timer armed";
    } else if (state.state === "idle") {
      $("status-reset").textContent = "No reset scheduled";
    } else if (state.state === "disabled") {
      $("status-reset").textContent = "Detection is paused";
    } else if (state.state === "sent") {
      $("status-reset").textContent = "Message delivered";
    } else {
      $("status-reset").textContent = "No reset scheduled";
    }

    if (ui.sendPending && STATE_TERMINAL.has(state.state)) {
      resetSendButton();
    }
    tickCountdown();
  }

  function renderActivity() {
    const list = $("activity-list");
    list.innerHTML = "";
    if (!ui.activity.length) {
      list.innerHTML =
        '<div class="activity-empty">Nothing yet. The app will log what it does here.</div>';
      return;
    }
    ui.activity.slice(-120).forEach((entry) => {
      list.appendChild(activityRow(entry));
    });
    list.scrollTop = list.scrollHeight;
  }

  function activityRow(entry) {
    const row = document.createElement("div");
    row.className = "activity-row";
    const time = document.createElement("span");
    time.className = "activity-time";
    time.textContent = entry.time || timeStamp();
    const message = document.createElement("span");
    message.textContent = entry.message || "";
    row.append(time, message);
    return row;
  }

  function appendActivity(entry) {
    if (!entry || !entry.message) return;
    ui.activity.push(entry);
    if (ui.activity.length > 120) ui.activity = ui.activity.slice(-120);
    const list = $("activity-list");
    const empty = list.querySelector(".activity-empty");
    if (empty) empty.remove();
    const nearBottom =
      list.scrollHeight - list.scrollTop - list.clientHeight < 48;
    list.appendChild(activityRow(entry));
    while (list.children.length > 120) list.removeChild(list.firstElementChild);
    if (nearBottom) list.scrollTop = list.scrollHeight;
  }

  function tickCountdown() {
    const label = $("countdown");
    const caption = $("countdown-caption");
    const state = ui.state || {};
    if (state.state === "disabled") {
      label.textContent = "Paused";
      label.className = "countdown text-gray";
      caption.textContent = "detection is off";
      return;
    }
    if (state.state === "starting" || !ui.bootstrapDone) {
      label.textContent = "—";
      label.className = "countdown";
      caption.textContent = "starting";
      return;
    }

    let target = parseDate(state.reset_at);
    const retry = parseDate(state.retry_at);
    let retrying = false;
    if (target && target.getTime() <= Date.now() && retry) {
      target = retry;
      retrying = true;
    }

    if (!target) {
      label.textContent = "—";
      label.className = `countdown ${state.state === "not_found" ? "text-gray" : ""}`;
      const captions = {
        idle: "no active limit",
        sent: "message sent",
        sending: "sending now",
        not_found: "waiting for the app",
        not_ready: "waiting for the app",
        error: "needs attention",
        deferred: "waiting for you",
        starting: "starting",
      };
      caption.textContent = captions[state.state] || "no active limit";
      return;
    }

    const remaining = Math.floor((target.getTime() - Date.now()) / 1000);
    if (remaining <= 0) {
      label.textContent = "Ready";
      label.className = "countdown text-green";
      caption.textContent = "checking the app now";
      return;
    }

    const hours = Math.floor(remaining / 3600);
    const minutes = Math.floor((remaining % 3600) / 60);
    const seconds = remaining % 60;
    label.textContent = hours
      ? `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
      : `${pad(minutes)}:${pad(seconds)}`;
    label.className = "countdown";
    caption.textContent = retrying ? "retrying after reset" : "until reset";
  }

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function timeStamp() {
    const now = new Date();
    return `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  }

  function titleCase(value) {
    return String(value || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  /* -------------------------------------------------------------------------
     Config writes
  ------------------------------------------------------------------------- */

  async function saveConfig(changes, options = {}) {
    if (!ui.config) return null;
    try {
      const result = await api("/api/config", { method: "POST", body: changes });
      if (result && result.config) {
        ui.config = result.config;
        if (!options.silent) renderConfig(result.config, { preserveFocus: true });
      }
      if (!options.silent) settingsSaved();
      return result ? result.config : null;
    } catch (error) {
      console.error(error);
      showToast(`Could not save: ${error.message}`, true);
      if (ui.config) renderConfig(ui.config, { preserveFocus: true });
      return null;
    }
  }

  function settingsSaved() {
    const status = $("settings-status");
    status.textContent = "Saved just now";
    status.className = "page-note text-green";
    if (ui.settingsTimer) clearTimeout(ui.settingsTimer);
    ui.settingsTimer = setTimeout(() => {
      status.textContent = "Changes save automatically";
      status.className = "page-note";
    }, 2200);
  }

  function showMessageSaved(text, color = "green", ms = 1600) {
    const note = $("message-saved");
    note.textContent = text;
    note.className = `muted-note ${color === "green" ? "text-green" : color === "accent" ? "text-blue" : ""}`;
    ui.lastMessageSaved += 1;
    const token = ui.lastMessageSaved;
    setTimeout(() => {
      if (token !== ui.lastMessageSaved) return;
      note.textContent = "Draft saves automatically";
      note.className = "muted-note";
    }, ms);
  }

  async function saveDraft(immediate = false) {
    if (!ui.config) return;
    const value = $("message-box").value.trim();
    if (!value) return;
    if (ui.messageTimer) {
      clearTimeout(ui.messageTimer);
      ui.messageTimer = null;
    }
    if (!immediate) {
      ui.messageTimer = setTimeout(() => saveDraft(true), 700);
      return;
    }
    if (value === ui.config.message) {
      showMessageSaved("Saved just now", "green");
      return;
    }
    const config = await saveConfig({ message: value }, { silent: true });
    if (config) {
      ui.config.message = config.message;
      if ($("message-box").value.trim() === config.message) {
        $("message-box").dataset.dirty = "0";
      }
      showMessageSaved("Saved just now", "green");
    }
  }

  function resetSendButton() {
    ui.sendPending = false;
    if (ui.sendTimer) {
      clearTimeout(ui.sendTimer);
      ui.sendTimer = null;
    }
    const button = $("send-now");
    button.disabled = false;
    button.textContent = "Send now";
  }

  async function sendNow() {
    if (ui.sendPending) return;
    const message = $("message-box").value.trim();
    if (!message) {
      showToast("Write a message before sending.");
      return;
    }
    ui.sendPending = true;
    const button = $("send-now");
    button.disabled = true;
    button.textContent = "Sending…";
    if (ui.sendTimer) clearTimeout(ui.sendTimer);
    ui.sendTimer = setTimeout(resetSendButton, 120000);
    try {
      const result = await api("/api/send", {
        method: "POST",
        body: { message },
      });
      if (result && result.config) {
        ui.config = result.config;
        renderConfig(result.config, { preserveFocus: true });
      }
    } catch (error) {
      showToast(`Could not send: ${error.message}`, true);
      resetSendButton();
    }
  }

  async function setDetection(enabled) {
    setSwitch($("detection-switch"), enabled);
    $("detection-label").textContent = enabled ? "Detection on" : "Detection off";
    try {
      const result = await api("/api/detection", {
        method: "POST",
        body: { enabled },
      });
      if (result && result.config) {
        ui.config = result.config;
        renderConfig(result.config, { preserveFocus: true });
      }
    } catch (error) {
      setSwitch($("detection-switch"), !enabled);
      $("detection-label").textContent = !enabled ? "Detection on" : "Detection off";
      if (invoke) {
        invoke("set_tray_detection", { enabled: !enabled }).catch(() => {});
      }
      showToast(`Could not change detection: ${error.message}`, true);
    }
  }

  async function refreshChats(silent = false) {
    if (!silent) {
      const button = $("refresh-chats");
      button.disabled = true;
      button.textContent = "Reading…";
      $("chat-hint").textContent = "Reading the sidebar…";
      if (ui.chatsTimer) clearTimeout(ui.chatsTimer);
      ui.chatsTimer = setTimeout(() => {
        button.disabled = false;
        button.textContent = "Refresh";
      }, 5000);
    }
    try {
      await api("/api/chats/refresh", { method: "POST", body: {} });
    } catch (error) {
      if (!silent) showToast(`Could not read chats: ${error.message}`, true);
    }
  }

  async function addPaths(paths) {
    if (!Array.isArray(paths) || !paths.length) return;
    try {
      const result = await api("/api/attachments", {
        method: "POST",
        body: { action: "add", paths },
      });
      if (result.attachments) {
        ui.config.attachments = result.attachments;
        renderAttachments(true);
      }
      if (result.message) {
        $("attachment-hint").textContent = result.message;
        $("attachment-hint").className = result.added
          ? "muted-note text-green"
          : "muted-note text-orange";
        if (result.added) showToast(result.message);
      } else {
        $("attachment-hint").textContent = "No new images added.";
        $("attachment-hint").className = "muted-note text-orange";
      }
    } catch (error) {
      showToast(`Could not add images: ${error.message}`, true);
    }
  }

  async function pickImages() {
    if (!invoke) {
      showToast("Image picking is available in the desktop app.");
      return;
    }
    try {
      const paths = await invoke("pick_images");
      if (Array.isArray(paths) && paths.length) await addPaths(paths);
    } catch (error) {
      showToast(`Could not open the image picker: ${error.message}`, true);
    }
  }

  async function pasteImages() {
    try {
      const result = await api("/api/attachments", {
        method: "POST",
        body: { action: "paste" },
      });
      if (result.attachments) {
        ui.config.attachments = result.attachments;
        renderAttachments(true);
      }
      const text = result.message || "No image found on the clipboard.";
      $("attachment-hint").textContent = text;
      $("attachment-hint").className = result.added
        ? "muted-note text-green"
        : "muted-note";
    } catch (error) {
      showToast(`Could not read the clipboard: ${error.message}`, true);
    }
  }

  async function removeAttachment(path) {
    try {
      const result = await api("/api/attachments", {
        method: "POST",
        body: { action: "remove", path },
      });
      if (result.attachments) {
        ui.config.attachments = result.attachments;
        renderAttachments(true);
      }
      $("attachment-hint").textContent = "Image removed.";
    } catch (error) {
      showToast(`Could not remove the image: ${error.message}`, true);
    }
  }

  /* -------------------------------------------------------------------------
     Appearance
  ------------------------------------------------------------------------- */

  function applyTheme(theme, persist = false) {
    const value = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = value;
    setSwitch($("light-switch"), value === "light");
    if (invoke) {
      invoke("set_window_theme", { theme: value }).catch(() => {});
    }
    if (persist) {
      saveConfig({ theme: value }, { silent: true });
      appendActivity({
        time: timeStamp(),
        message:
          value === "light" ? "Light theme enabled." : "Dark theme enabled.",
      });
    }
  }

  function applyScale(scale, persist = false) {
    const value = UI_SCALES.includes(Number(scale)) ? Number(scale) : 100;
    document.documentElement.style.setProperty("--ui-scale", value / 100);
    document.querySelectorAll("[data-scale]").forEach((button) => {
      button.classList.toggle(
        "is-active",
        Number(button.dataset.scale) === value,
      );
    });
    if (invoke) {
      invoke("apply_ui_scale", { scale: value }).catch(() => {});
    }
    if (persist) {
      saveConfig({ ui_scale: value }, { silent: true });
      appendActivity({
        time: timeStamp(),
        message: `Interface scale set to ${value}%.`,
      });
    }
  }

  async function setCloseToTray(enabled) {
    if (!invoke) return;
    try {
      await invoke("set_close_to_tray", { enabled: Boolean(enabled) });
    } catch (error) {
      console.warn("Could not set close-to-tray", error);
    }
  }

  async function syncStartup(config) {
    if (!invoke || ui.startupSynced) return;
    ui.startupSynced = true;
    try {
      const enabled = await invoke("startup_enabled");
      if (config.start_with_windows && !enabled) {
        const ok = await invoke("set_startup", { enabled: true });
        if (!ok) {
          ui.config.start_with_windows = false;
          setSwitch($("startup-switch"), false);
        }
      } else if (!config.start_with_windows && enabled) {
        // The shortcut exists but the config was reset or migrated.
        config.start_with_windows = true;
        await saveConfig({ start_with_windows: true }, { silent: true });
        setSwitch($("startup-switch"), true);
      }
    } catch (error) {
      console.warn("Could not read the startup setting", error);
    }
  }

  async function toggleStartup(enabled) {
    if (!invoke) {
      showToast("Startup is controlled by the desktop app.");
      setSwitch($("startup-switch"), !enabled);
      return;
    }
    try {
      const result = await invoke("set_startup", { enabled });
      if (Boolean(result) !== Boolean(enabled)) {
        throw new Error("The startup entry could not be updated.");
      }
      await saveConfig({ start_with_windows: enabled }, { silent: true });
      settingsSaved();
    } catch (error) {
      setSwitch($("startup-switch"), !enabled);
      showToast(error.message, true);
    }
  }

  /* -------------------------------------------------------------------------
     Event binding
  ------------------------------------------------------------------------- */

  function bindPageNavigation() {
    document.querySelectorAll(".nav-item").forEach((button) => {
      button.addEventListener("click", () => showPage(button.dataset.page));
    });
  }

  function showPage(page) {
    if (!PAGE_META[page]) return;
    ui.page = page;
    document.querySelectorAll(".nav-item").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.page === page);
    });
    document.querySelectorAll(".page").forEach((section) => {
      section.classList.toggle("is-active", section.id === `page-${page}`);
    });
    $("page-title").textContent = PAGE_META[page][0];
    $("page-subtitle").textContent = PAGE_META[page][1];
    if (page === "settings" && !ui.chats.length) {
      refreshChats(true);
    }
  }

  function bindSwitch(id, handler) {
    const element = $(id);
    element.addEventListener("click", () => {
      const next = !isSwitchOn(element);
      setSwitch(element, next);
      handler(next);
    });
  }

  function bindStaticHandlers() {
    bindSwitch("detection-switch", (value) => setDetection(value));
    bindSwitch("dev-mode-switch", (value) =>
      saveConfig({ claude_dev_mode: value }),
    );
    bindSwitch("startup-switch", (value) => toggleStartup(value));
    bindSwitch("tray-switch", (value) => {
      saveConfig({ minimize_to_tray: value });
      setCloseToTray(value);
    });
    bindSwitch("launch-switch", (value) =>
      saveConfig({ launch_if_missing: value }),
    );
    bindSwitch("drafts-switch", (value) =>
      saveConfig({ preserve_drafts: value }),
    );
    bindSwitch("active-switch", (value) =>
      saveConfig({ defer_when_active: value }),
    );
    bindSwitch("locked-switch", (value) =>
      saveConfig({ pause_when_locked: value }),
    );
    bindSwitch("light-switch", (value) =>
      applyTheme(value ? "light" : "dark", true),
    );

    document.querySelectorAll("[data-target]").forEach((button) => {
      button.addEventListener("click", async () => {
        const target = button.dataset.target;
        if (!APP_LABELS[target]) return;
        await saveConfig({ app_target: target });
        appendActivity({
          time: timeStamp(),
          message: `Target app set to ${APP_LABELS[target]}.`,
        });
        refreshChats(true);
      });
    });

    document.querySelectorAll("[data-scale]").forEach((button) => {
      button.addEventListener("click", () => {
        applyScale(Number(button.dataset.scale), true);
      });
    });

    $("send-now").addEventListener("click", sendNow);

    $("message-box").addEventListener("input", () => {
      $("message-box").dataset.dirty = "1";
      saveDraft(false);
    });
    $("message-box").addEventListener("blur", () => saveDraft(true));

    $("preset-select").addEventListener("change", (event) => {
      const value = event.target.value;
      if (!value) return;
      $("message-box").value = value;
      $("message-box").dataset.dirty = "0";
      saveConfig({ message: value }, { silent: true });
      showMessageSaved("Preset loaded", "accent", 1400);
      event.target.value = "";
    });

    $("save-preset").addEventListener("click", async () => {
      const text = $("message-box").value.trim();
      if (!text) return;
      const saved = ui.config ? ui.config.saved_messages || [] : [];
      const next = saved.includes(text) ? saved : [...saved, text];
      await saveConfig({ message: text, saved_messages: next });
      renderPresets(true);
      showMessageSaved("Preset saved", "green", 1600);
    });

    $("refresh-chats").addEventListener("click", () => refreshChats(false));
    $("chat-select").addEventListener("change", (event) => {
      const name = event.target.value;
      saveConfig({ chat_name: name });
      appendActivity({
        time: timeStamp(),
        message: name
          ? `Chat set to "${name}".`
          : "Using the currently open chat.",
      });
    });

    $("paste-images").addEventListener("click", pasteImages);
    $("add-images").addEventListener("click", pickImages);

    $("clear-activity").addEventListener("click", () => {
      ui.activity = [];
      renderActivity();
    });

    document.querySelectorAll("[data-url]").forEach((button) => {
      button.addEventListener("click", () => openUrl(button.dataset.url));
    });

    document.querySelectorAll("[data-open]").forEach((button) => {
      button.addEventListener("click", () => openTarget(button.dataset.open));
    });

    $("read-license").addEventListener("click", showLicense);
    $("license-close").addEventListener("click", () => {
      $("license-dialog").close();
    });
    $("license-dialog").addEventListener("click", (event) => {
      if (event.target === $("license-dialog")) $("license-dialog").close();
    });

    $("interval-input").addEventListener("change", saveNumericSettings);
    $("wake-input").addEventListener("change", saveNumericSettings);
    $("grace-input").addEventListener("change", saveNumericSettings);
    $("interval-input").addEventListener("keydown", submitOnEnter);
    $("wake-input").addEventListener("keydown", submitOnEnter);
    $("grace-input").addEventListener("keydown", submitOnEnter);

    document.addEventListener("keydown", (event) => {
      if (
        (event.ctrlKey || event.metaKey) &&
        event.key.toLowerCase() === "v"
      ) {
        const tag = (event.target && event.target.tagName) || "";
        if (tag !== "TEXTAREA" && tag !== "INPUT") {
          event.preventDefault();
          pasteImages();
        }
      }
    });
  }

  function submitOnEnter(event) {
    if (event.key === "Enter") {
      event.preventDefault();
      event.target.blur();
    }
  }

  async function saveNumericSettings() {
    if (!ui.config) return;
    const minutes = Number.parseInt($("interval-input").value, 10);
    if (!Number.isFinite(minutes) || minutes < 1 || minutes > 1440) {
      $("interval-input").value = String(
        Math.max(1, Math.round((ui.config.poll_seconds || 900) / 60)),
      );
      showToast("Check interval must be 1–1440 minutes.", true);
      return;
    }
    const wake = Number.parseInt($("wake-input").value, 10);
    if (!Number.isFinite(wake) || wake < 0 || wake > 300) {
      $("wake-input").value = String(ui.config.wake_early_seconds ?? 10);
      showToast("Wake early must be 0–300 seconds.", true);
      return;
    }
    const grace = Number.parseInt($("grace-input").value, 10);
    if (!Number.isFinite(grace) || grace < 0 || grace > 3600) {
      $("grace-input").value = String(ui.config.active_grace_seconds ?? 20);
      showToast("Idle grace must be 0–3600 seconds.", true);
      return;
    }
    await saveConfig({
      poll_seconds: minutes * 60,
      wake_early_seconds: wake,
      active_grace_seconds: grace,
    });
  }

  async function openUrl(url) {
    if (!url) return;
    try {
      await api("/api/open-url", { method: "POST", body: { url } });
    } catch (error) {
      window.open(url, "_blank", "noopener");
    }
  }

  async function openTarget(target) {
    try {
      await api("/api/open", { method: "POST", body: { target } });
    } catch (error) {
      showToast(`Could not open that folder: ${error.message}`, true);
    }
  }

  async function showLicense() {
    const dialog = $("license-dialog");
    const body = $("license-body");
    try {
      const response = await fetch(
        `${ui.apiBase}/api/license${ui.token ? `?token=${encodeURIComponent(ui.token)}` : ""}`,
        { cache: "no-store" },
      );
      body.textContent = response.ok
        ? await response.text()
        : "The license text could not be loaded.";
    } catch (error) {
      body.textContent = "The license text could not be loaded.";
    }
    dialog.showModal();
    body.scrollTop = 0;
    body.focus();
  }

  function showToast(message, isError = false) {
    const toast = $("toast");
    toast.textContent = message;
    toast.className = `toast is-visible${isError ? " is-error" : ""}`;
    if (ui.toastTimer) clearTimeout(ui.toastTimer);
    ui.toastTimer = setTimeout(() => {
      toast.className = "toast";
    }, 2600);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
