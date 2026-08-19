const SERVER = "http://127.0.0.1:4599";
const POLL_MS = 700;

const els = {
  url: document.getElementById("url"),
  serverWarning: document.getElementById("serverWarning"),
  ffmpegWarning: document.getElementById("ffmpegWarning"),
  videoCard: document.getElementById("videoCard"),
  videoThumb: document.getElementById("videoThumb"),
  videoTitle: document.getElementById("videoTitle"),
  videoMeta: document.getElementById("videoMeta"),
  quality: document.getElementById("qualitySelect"),
  qualityHint: document.getElementById("qualityHint"),
  optThumbnail: document.getElementById("optThumbnail"),
  optSubtitles: document.getElementById("optSubtitles"),
  optMetadata: document.getElementById("optMetadata"),
  downloadBtn: document.getElementById("downloadBtn"),
  cancelBtn: document.getElementById("cancelBtn"),
  progressWrap: document.getElementById("progressWrap"),
  progressFill: document.getElementById("progressFill"),
  status: document.getElementById("status"),
  historyBtn: document.getElementById("historyBtn"),
};

let currentUrl = null;
let meta = {};
let presets = [];
let formats = [];
let activeJobId = null;
let pollTimer = null;

// --------------------------------------------------------------------------- //
// helpers
// --------------------------------------------------------------------------- //

function setStatus(text, kind = "") {
  els.status.textContent = text || "";
  els.status.className = `status ${kind}`;
}

function setProgress(percent) {
  if (percent === null || percent === undefined) {
    els.progressWrap.hidden = true;
    return;
  }
  els.progressWrap.hidden = false;
  els.progressFill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
}

function humanSize(bytes) {
  if (!bytes) return null;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes, i = 0;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(1)} ${units[i]}`;
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

function humanEta(seconds) {
  if (!seconds || seconds < 0) return null;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  return `${h}h ${Math.round((seconds % 3600) / 60)}m`;
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
// preferences
// --------------------------------------------------------------------------- //

const PREF_KEYS = ["quality", "embed_thumbnail", "subtitles", "embed_metadata"];

async function loadPrefs() {
  const stored = await chrome.storage.local.get(PREF_KEYS);
  els.optThumbnail.checked = stored.embed_thumbnail ?? false;
  els.optSubtitles.checked = stored.subtitles ?? false;
  els.optMetadata.checked = stored.embed_metadata ?? true;
  return stored.quality || null;
}

function savePrefs() {
  chrome.storage.local.set({
    quality: els.quality.value,
    embed_thumbnail: els.optThumbnail.checked,
    subtitles: els.optSubtitles.checked,
    embed_metadata: els.optMetadata.checked,
  });
}

// --------------------------------------------------------------------------- //
// rendering
// --------------------------------------------------------------------------- //

function renderVideoCard(data) {
  if (data.thumbnail) {
    els.videoThumb.src = data.thumbnail;
    els.videoThumb.onerror = () => { els.videoThumb.style.display = "none"; };
  } else {
    els.videoThumb.style.display = "none";
  }
  els.videoTitle.textContent = data.title || "Untitled";
  const bits = [data.uploader, humanDuration(data.duration)].filter(Boolean);
  els.videoMeta.textContent = bits.join(" · ");
  els.videoCard.hidden = false;
  els.url.textContent = data.webpage_url || currentUrl;
}

function renderQualityOptions(data, preferred) {
  presets = (data.presets || []).filter((p) => p.available);
  formats = data.formats || [];
  els.quality.innerHTML = "";

  const presetGroup = document.createElement("optgroup");
  presetGroup.label = "Presets";
  for (const p of presets) {
    const opt = document.createElement("option");
    opt.value = `preset:${p.key}`;
    const detail = [p.detail, p.filesize_human, p.ext].filter(Boolean).join(" · ");
    opt.textContent = detail ? `${p.label} — ${detail}` : p.label;
    presetGroup.appendChild(opt);
  }
  els.quality.appendChild(presetGroup);

  if (formats.length) {
    const formatGroup = document.createElement("optgroup");
    formatGroup.label = "Exact format";
    for (const f of formats) {
      const opt = document.createElement("option");
      opt.value = `format:${f.format_id}`;
      const bits = [
        f.resolution,
        f.vcodec || f.acodec,
        f.ext,
        humanSize(f.filesize),
        f.kind === "video only" ? "+ best audio" : null,
      ].filter(Boolean);
      opt.textContent = `${f.format_id} · ${bits.join(" · ")}`;
      formatGroup.appendChild(opt);
    }
    els.quality.appendChild(formatGroup);
  }

  // Restore the last choice when this video still offers it.
  const wanted = preferred && [...els.quality.options].some((o) => o.value === preferred)
    ? preferred
    : `preset:${data.default_quality || "max"}`;
  els.quality.value = wanted;
  if (!els.quality.value) els.quality.selectedIndex = 0;

  els.quality.disabled = false;
  els.downloadBtn.disabled = false;
  updateQualityHint();
}

function selectedChoice() {
  const value = els.quality.value || "";
  if (value.startsWith("preset:")) {
    const key = value.slice(7);
    return { kind: "preset", key, preset: presets.find((p) => p.key === key) };
  }
  const id = value.slice(7);
  return { kind: "format", id, format: formats.find((f) => f.format_id === id) };
}

function updateQualityHint() {
  const choice = selectedChoice();
  if (choice.kind === "preset" && choice.preset) {
    const p = choice.preset;
    const size = p.filesize ? humanSize(p.filesize) : "unknown size";
    els.qualityHint.textContent = p.format_ids
      ? `yt-dlp format ${p.format_ids.join("+")} → ${p.ext || "?"}, ${size}`
      : size;
  } else if (choice.kind === "format" && choice.format) {
    const f = choice.format;
    els.qualityHint.textContent = f.needs_audio
      ? "Video-only stream — best audio will be merged in automatically."
      : `${f.kind}, ${humanSize(f.filesize) || "unknown size"}`;
  } else {
    els.qualityHint.textContent = "";
  }
}

// --------------------------------------------------------------------------- //
// download lifecycle
// --------------------------------------------------------------------------- //

function showDownloadingUi(on) {
  els.downloadBtn.hidden = on;
  els.cancelBtn.hidden = !on;
  els.quality.disabled = on;
}

function describeJob(job) {
  if (job.status === "queued") return "Queued — waiting for a free slot…";
  if (job.status === "processing") return job.stage || "Merging / finalizing…";
  if (job.status === "downloading") {
    const bits = [];
    if (job.percent) bits.push(job.percent);
    if (job.speed) bits.push(job.speed);
    const eta = humanEta(job.eta);
    if (eta) bits.push(`${eta} left`);
    if (job.total_bytes) {
      bits.push(`${humanSize(job.downloaded_bytes)} of ${humanSize(job.total_bytes)}`);
    }
    return bits.join("  ·  ") || "Downloading…";
  }
  return "";
}

async function pollProgress() {
  if (!activeJobId) return;
  let job;
  try {
    job = await api(`/progress/${activeJobId}`);
  } catch (e) {
    // A transient failure shouldn't silently kill the progress display.
    setStatus(`Lost contact with the server: ${e.message}`, "error");
    pollTimer = setTimeout(pollProgress, 2000);
    return;
  }

  if (job.status === "done") {
    setProgress(100);
    setStatus("Done. Saved to your downloads folder.", "success");
    showDownloadingUi(false);
    activeJobId = null;
    return;
  }
  if (job.status === "error") {
    setProgress(null);
    setStatus(job.error || "Download failed.", "error");
    showDownloadingUi(false);
    activeJobId = null;
    return;
  }
  if (job.status === "cancelled") {
    setProgress(null);
    setStatus("Cancelled.");
    showDownloadingUi(false);
    activeJobId = null;
    return;
  }

  setProgress(job.percent_value ?? 0);
  setStatus(describeJob(job));
  pollTimer = setTimeout(pollProgress, POLL_MS);
}

async function startDownload() {
  const choice = selectedChoice();
  const body = {
    url: currentUrl,
    title: meta.title,
    thumbnail: meta.thumbnail,
    uploader: meta.uploader,
    duration: meta.duration,
    embed_thumbnail: els.optThumbnail.checked,
    embed_metadata: els.optMetadata.checked,
    subtitles: els.optSubtitles.checked,
  };

  if (choice.kind === "preset") {
    body.quality = choice.key;
    body.resolution_label = choice.preset
      ? [choice.preset.label, choice.preset.detail].filter(Boolean).join(" — ")
      : null;
  } else {
    body.format_id = choice.id;
    body.needs_audio = !!(choice.format && choice.format.needs_audio);
    body.resolution_label = choice.format
      ? `${choice.format.resolution} · ${choice.format.ext}`
      : null;
  }

  savePrefs();
  showDownloadingUi(true);
  setStatus("Starting…");
  setProgress(0);

  try {
    const data = await postJson("/download", body);
    activeJobId = data.job_id;
    if (data.queued_behind > 0) {
      setStatus(`Queued behind ${data.queued_behind} other download(s)…`);
    }
    pollProgress();
  } catch (e) {
    setStatus(`Could not start: ${e.message}`, "error");
    setProgress(null);
    showDownloadingUi(false);
  }
}

async function cancelDownload() {
  if (!activeJobId) return;
  els.cancelBtn.disabled = true;
  try {
    await postJson("/cancel", { job_id: activeJobId });
    setStatus("Cancelling…");
  } catch (e) {
    setStatus(e.message, "error");
  } finally {
    els.cancelBtn.disabled = false;
  }
}

/** Reattach to a download already running for this URL, so closing and
 *  reopening the popup doesn't look like the download vanished. */
async function reattachActiveJob() {
  try {
    const history = await api("/history");
    const active = history.items.find(
      (r) => ["queued", "downloading", "processing"].includes(r.status)
             && r.job_id && (!currentUrl || r.url === currentUrl));
    if (!active) return false;
    activeJobId = active.job_id;
    showDownloadingUi(true);
    setStatus("Reconnected to a download already in progress…");
    pollProgress();
    return true;
  } catch (e) {
    return false;
  }
}

// --------------------------------------------------------------------------- //
// init
// --------------------------------------------------------------------------- //

async function init() {
  const preferredQuality = await loadPrefs();

  let health;
  try {
    health = await api("/ping");
  } catch (e) {
    els.serverWarning.hidden = false;
    els.url.textContent = "";
    setStatus("Start the helper server, then reopen this popup.", "error");
    return;
  }
  els.ffmpegWarning.hidden = !!health.ffmpeg;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentUrl = tab && tab.url ? tab.url : null;

  if (!currentUrl || !/^https?:\/\//.test(currentUrl)) {
    els.url.textContent = "This tab has no downloadable page URL.";
    setStatus("Open a video page and reopen this popup.");
    await reattachActiveJob();
    return;
  }
  els.url.textContent = currentUrl;

  const reattached = await reattachActiveJob();

  setStatus("Reading available formats…");
  try {
    const data = await api(`/formats?url=${encodeURIComponent(currentUrl)}`);
    meta = {
      title: data.title,
      thumbnail: data.thumbnail,
      uploader: data.uploader,
      duration: data.duration,
    };
    renderVideoCard(data);
    renderQualityOptions(data, preferredQuality);
    if (!data.ffmpeg) els.ffmpegWarning.hidden = false;
    if (!reattached) {
      setStatus(data.is_live
        ? "This is a live stream — downloading captures it from now on."
        : "");
    }
    if (reattached) els.quality.disabled = true;
  } catch (e) {
    setStatus(`Could not read formats: ${e.message}`, "error");
  }
}

els.quality.addEventListener("change", () => { updateQualityHint(); savePrefs(); });
els.downloadBtn.addEventListener("click", startDownload);
els.cancelBtn.addEventListener("click", cancelDownload);
for (const el of [els.optThumbnail, els.optSubtitles, els.optMetadata]) {
  el.addEventListener("change", savePrefs);
}
els.historyBtn.addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("dashboard.html") });
});
window.addEventListener("unload", () => clearTimeout(pollTimer));

init();
