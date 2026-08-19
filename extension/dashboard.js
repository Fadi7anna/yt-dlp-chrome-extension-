const SERVER = "http://127.0.0.1:4599";
const POLL_MS = 1500;
const VERSION_POLL_MS = 15000;

const els = {
  warning: document.getElementById("serverWarning"),
  ffmpegWarning: document.getElementById("ffmpegWarning"),
  loading: document.getElementById("loading"),
  empty: document.getElementById("empty"),
  grid: document.getElementById("grid"),
  search: document.getElementById("search"),
  pills: document.getElementById("filterPills"),
  sort: document.getElementById("sortSelect"),
  clearFailedBtn: document.getElementById("clearFailedBtn"),
  statCount: document.getElementById("statCount"),
  statSize: document.getElementById("statSize"),
  statActive: document.getElementById("statActive"),
  openFolderBtn: document.getElementById("openFolderBtn"),
  updateBtn: document.getElementById("updateBtn"),
  updateBtnLabel: document.getElementById("updateBtnLabel"),
  versionBadge: document.getElementById("versionBadge"),
  toast: document.getElementById("toast"),
};

const state = {
  items: [],
  filter: "all",
  query: "",
  sort: "newest",
};

const ACTIVE = ["queued", "downloading", "processing"];

const ICONS = {
  open: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>`,
  folder: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>`,
  redo: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 4v6h6M20 20v-6h-6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M5 13a7 7 0 0012.9 3.5M19 11A7 7 0 006.1 7.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  trash: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 7h16M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2m2 0v13a1 1 0 01-1 1H8a1 1 0 01-1-1V7h10z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  stop: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor"/></svg>`,
};

// --------------------------------------------------------------------------- //
// formatting
// --------------------------------------------------------------------------- //

function showToast(message, type = "") {
  els.toast.textContent = message;
  els.toast.className = `toast ${type}`;
  els.toast.hidden = false;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => (els.toast.hidden = true), 3500);
}

function humanSize(bytes) {
  if (!bytes) return null;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes, i = 0;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(1)}${units[i]}`;
}

function humanDuration(seconds) {
  if (!seconds && seconds !== 0) return null;
  seconds = Math.round(seconds);
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  const d = Math.floor(diff / 86400);
  if (d < 30) return `${d}d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

function statusLabel(status) {
  return {
    queued: "Queued",
    downloading: "Downloading",
    processing: "Processing",
    done: "Done",
    error: "Failed",
    cancelled: "Cancelled",
  }[status] || status;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

async function api(path, opts) {
  const res = await fetch(`${SERVER}${path}`, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function postJson(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

// --------------------------------------------------------------------------- //
// filtering
// --------------------------------------------------------------------------- //

function matchesFilter(item) {
  if (state.filter === "all") return true;
  if (state.filter === "downloading") return ACTIVE.includes(item.status);
  if (state.filter === "error") return item.status === "error" || item.status === "cancelled";
  return item.status === state.filter;
}

function matchesQuery(item) {
  if (!state.query) return true;
  const q = state.query.toLowerCase();
  return (item.title || "").toLowerCase().includes(q)
      || (item.uploader || "").toLowerCase().includes(q);
}

function sortItems(items) {
  const arr = [...items];
  switch (state.sort) {
    case "oldest": return arr.sort((a, b) => a.created_at - b.created_at);
    case "title": return arr.sort((a, b) => (a.title || "").localeCompare(b.title || ""));
    case "size": return arr.sort((a, b) => (b.filesize || 0) - (a.filesize || 0));
    default: return arr.sort((a, b) => b.created_at - a.created_at);
  }
}

// --------------------------------------------------------------------------- //
// rendering
//
// Cards are built once and then patched in place. Re-assigning grid.innerHTML on
// every 1.5s poll (as this used to) rebuilt every node, which reset hover state,
// dropped focus, and made the thumbnails flicker on each tick.
// --------------------------------------------------------------------------- //

function buildCard(item) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.id = item.id;
  card.innerHTML = `
    <div class="card-thumb">
      <span class="status-badge"></span>
      <span class="duration-badge" hidden></span>
      <div class="progress-track" hidden><div class="progress-fill"></div></div>
    </div>
    <div class="card-body">
      <div class="card-title"></div>
      <div class="card-meta"></div>
      <div class="card-progress-text" hidden></div>
      <div class="card-stage" hidden></div>
      <div class="card-error" hidden></div>
      <span class="card-quality" hidden></span>
    </div>
    <div class="card-actions">
      <button class="btn-ghost" data-action="open" title="Open file">${ICONS.open}</button>
      <button class="btn-ghost" data-action="folder" title="Show in folder">${ICONS.folder}</button>
      <button class="btn-ghost btn-cancel" data-action="cancel" title="Cancel download">${ICONS.stop}</button>
      <button class="btn-ghost" data-action="redownload" title="Download again">${ICONS.redo}</button>
      <button class="btn-danger-ghost" data-action="delete" title="Delete">${ICONS.trash}</button>
    </div>
  `;
  return card;
}

function patchCard(card, item) {
  const inProgress = ACTIVE.includes(item.status);
  const thumbBox = card.querySelector(".card-thumb");
  const badge = card.querySelector(".status-badge");
  const durationBadge = card.querySelector(".duration-badge");
  const track = card.querySelector(".progress-track");
  const fill = card.querySelector(".progress-fill");
  const title = card.querySelector(".card-title");
  const meta = card.querySelector(".card-meta");
  const progressText = card.querySelector(".card-progress-text");
  const stage = card.querySelector(".card-stage");
  const errorBox = card.querySelector(".card-error");
  const quality = card.querySelector(".card-quality");

  // Thumbnail: insert once, never re-set (re-setting src refetches and flickers).
  if (item.thumbnail && !card.querySelector(".card-thumb img")) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = item.thumbnail;
    img.addEventListener("error", () => {
      img.remove();
      thumbBox.classList.add("no-thumb");
    });
    thumbBox.prepend(img);
  } else if (!item.thumbnail) {
    thumbBox.classList.add("no-thumb");
  }

  badge.textContent = statusLabel(item.status);
  badge.className = `status-badge ${item.status}`;

  const duration = humanDuration(item.duration);
  durationBadge.hidden = !(duration && !inProgress);
  if (duration) durationBadge.textContent = duration;

  const pct = item.percent_value ?? parseFloat(item.percent);
  const showBar = item.status === "downloading" && !Number.isNaN(pct) && pct !== null;
  track.hidden = !showBar;
  if (showBar) fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;

  if (title.textContent !== (item.title || "")) {
    title.textContent = item.title || "Untitled";
    title.title = item.title || "";
  }

  const size = item.filesize_human || humanSize(item.filesize);
  const metaParts = [];
  if (item.uploader) metaParts.push(item.uploader);
  if (size && item.status === "done") metaParts.push(size);
  metaParts.push(timeAgo(item.created_at));
  const metaHtml = metaParts.filter(Boolean).map(escapeHtml)
    .join(' <span class="dot">·</span> ');
  if (meta.innerHTML !== metaHtml) meta.innerHTML = metaHtml;

  let progress = "";
  if (item.status === "downloading") {
    progress = [item.percent, item.speed].filter(Boolean).join(" · ");
  } else if (item.status === "queued") {
    progress = "Waiting for a free slot…";
  }
  progressText.hidden = !progress;
  progressText.textContent = progress;

  const stageText = item.status === "processing" ? (item.stage || "Merging / finalizing…") : "";
  stage.hidden = !stageText;
  stage.textContent = stageText;

  const errText = (item.status === "error" || item.status === "cancelled") ? item.error : "";
  errorBox.hidden = !errText;
  if (errText) errorBox.textContent = errText;

  quality.hidden = !item.resolution_label;
  if (item.resolution_label) quality.textContent = item.resolution_label;

  const canOpen = item.status === "done";
  card.querySelector('[data-action="open"]').disabled = !canOpen;
  card.querySelector('[data-action="folder"]').disabled = !canOpen;
  card.querySelector('[data-action="cancel"]').hidden = !inProgress;
  card.querySelector('[data-action="redownload"]').hidden = inProgress;
}

function render() {
  const filtered = sortItems(state.items.filter((i) => matchesFilter(i) && matchesQuery(i)));

  els.loading.hidden = true;

  if (state.items.length === 0) {
    els.empty.hidden = false;
    els.grid.hidden = true;
    els.grid.replaceChildren();
    return;
  }

  els.empty.hidden = true;
  els.grid.hidden = false;

  if (!filtered.length) {
    els.grid.replaceChildren();
    const note = document.createElement("div");
    note.className = "loading";
    note.style.gridColumn = "1/-1";
    note.textContent = "No downloads match your search.";
    els.grid.appendChild(note);
    return;
  }

  const existing = new Map(
    [...els.grid.children].filter((c) => c.dataset && c.dataset.id).map((c) => [c.dataset.id, c]));

  const ordered = filtered.map((item) => {
    const card = existing.get(item.id) || buildCard(item);
    existing.delete(item.id);
    patchCard(card, item);
    return card;
  });

  for (const stale of existing.values()) stale.remove();
  // Only touch the DOM order when it actually differs.
  const current = [...els.grid.children];
  const sameOrder = current.length === ordered.length
    && ordered.every((node, i) => current[i] === node);
  if (!sameOrder) els.grid.replaceChildren(...ordered);
}

function updateStats(data) {
  const totalSize = state.items.reduce((sum, i) => sum + (i.filesize || 0), 0);
  const active = state.items.filter((i) => ACTIVE.includes(i.status)).length;
  els.statCount.textContent = state.items.length;
  els.statSize.textContent = humanSize(totalSize) || "0B";
  els.statActive.textContent = active;
  if (data) els.ffmpegWarning.hidden = data.ffmpeg !== false;
}

// --------------------------------------------------------------------------- //
// data + actions
// --------------------------------------------------------------------------- //

async function refresh() {
  try {
    const data = await api("/history");
    state.items = data.items;
    els.warning.hidden = true;
    updateStats(data);
    render();
  } catch (e) {
    els.warning.hidden = false;
    els.loading.hidden = true;
  }
}

async function handleAction(action, id) {
  const item = state.items.find((i) => i.id === id);
  try {
    if (action === "open") {
      await postJson("/open_file", { id });
    } else if (action === "folder") {
      await postJson("/open_folder", { id });
    } else if (action === "cancel") {
      await postJson("/cancel", { id });
      showToast("Cancelling…");
      refresh();
    } else if (action === "redownload") {
      await postJson("/redownload", { id });
      showToast("Re-download started", "success");
      refresh();
    } else if (action === "delete") {
      const name = item ? item.title : "this download";
      const hasFile = item && item.filepath && item.status === "done";
      const message = hasFile
        ? `Delete "${name}"?\n\nThis removes the record and the file from disk.`
        : `Remove "${name}" from the history?`;
      if (!confirm(message)) return;
      await postJson("/delete", { id, delete_file: true });
      showToast("Deleted", "success");
      refresh();
    }
  } catch (e) {
    showToast(e.message, "error");
  }
}

els.grid.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn || btn.disabled) return;
  const card = e.target.closest(".card");
  if (card) handleAction(btn.dataset.action, card.dataset.id);
});

els.search.addEventListener("input", (e) => {
  state.query = e.target.value.trim();
  render();
});

els.pills.addEventListener("click", (e) => {
  const pill = e.target.closest(".pill");
  if (!pill) return;
  [...els.pills.children].forEach((p) => p.classList.remove("active"));
  pill.classList.add("active");
  state.filter = pill.dataset.filter;
  render();
});

els.sort.addEventListener("change", (e) => {
  state.sort = e.target.value;
  render();
});

els.openFolderBtn.addEventListener("click", async () => {
  try {
    await postJson("/open_downloads_folder");
  } catch (e) {
    showToast(e.message, "error");
  }
});

els.clearFailedBtn.addEventListener("click", async () => {
  const count = state.items.filter((i) => i.status === "error" || i.status === "cancelled").length;
  if (!count) return showToast("Nothing to clear");
  if (!confirm(`Remove ${count} failed/cancelled record(s) from the history?`)) return;
  try {
    // Two calls: the server clears one status per request.
    const a = await postJson("/clear_history", { status: "error" });
    const b = await postJson("/clear_history", { status: "cancelled" });
    showToast(`Cleared ${a.removed + b.removed} record(s)`, "success");
    refresh();
  } catch (e) {
    showToast(e.message, "error");
  }
});

// --------------------------------------------------------------------------- //
// version / update
// --------------------------------------------------------------------------- //

let pollingForRestart = false;

function resetUpdateButton(label = "Check for Updates") {
  els.updateBtn.classList.remove("spinning");
  els.updateBtn.disabled = false;
  els.updateBtnLabel.textContent = label;
}

async function refreshVersion() {
  try {
    const v = await api("/version");
    els.versionBadge.textContent = `v${v.current_version}`;
    const stale = v.latest_available && v.latest_available !== v.current_version;
    els.versionBadge.classList.toggle("stale", !!stale);
    els.versionBadge.title = stale
      ? `yt-dlp ${v.latest_available} is available`
      : "yt-dlp is up to date";

    if (v.checking) {
      els.updateBtn.classList.add("spinning");
      els.updateBtn.disabled = true;
      els.updateBtnLabel.textContent = "Checking…";
      return;
    }

    if (v.restart_pending) {
      els.updateBtnLabel.textContent = "Restarting after downloads…";
      return;
    }

    resetUpdateButton();
    if (pollingForRestart) {
      pollingForRestart = false;
      showToast(`Updated to v${v.current_version} and restarted`, "success");
    }
  } catch (e) {
    if (pollingForRestart) {
      // execv drops the listening socket briefly; keep polling.
      els.updateBtnLabel.textContent = "Restarting server…";
    }
  }
}

els.updateBtn.addEventListener("click", async () => {
  els.updateBtn.classList.add("spinning");
  els.updateBtn.disabled = true;
  els.updateBtnLabel.textContent = "Checking…";
  try {
    await postJson("/update");
    pollingForRestart = true;
    let tries = 0;
    const fast = setInterval(async () => {
      tries++;
      await refreshVersion();
      if (!pollingForRestart || tries > 60) {
        clearInterval(fast);
        if (pollingForRestart) { pollingForRestart = false; resetUpdateButton(); }
      }
    }, 1000);
  } catch (e) {
    resetUpdateButton();
    showToast(e.message, "error");
  }
});

// --------------------------------------------------------------------------- //
// polling
// --------------------------------------------------------------------------- //

let historyTimer = null;
let versionTimer = null;

function startPolling() {
  stopPolling();
  historyTimer = setInterval(refresh, POLL_MS);
  versionTimer = setInterval(refreshVersion, VERSION_POLL_MS);
}

function stopPolling() {
  clearInterval(historyTimer);
  clearInterval(versionTimer);
}

// A background tab has no reason to keep hitting the server every 1.5s.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPolling();
  } else {
    refresh();
    refreshVersion();
    startPolling();
  }
});

refresh();
refreshVersion();
startPolling();
