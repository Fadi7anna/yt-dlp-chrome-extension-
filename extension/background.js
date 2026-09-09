/**
 * Background service worker.
 *
 * Four jobs, all of which need to work with no popup open:
 *
 *  1. Keep the extension current. The helper server fast-forwards this
 *     checkout whenever it upgrades yt-dlp, but Chrome never re-reads an
 *     unpacked extension on its own -- it runs whatever it loaded until
 *     something calls chrome.runtime.reload(). That call is this file's, and
 *     it is what makes "always up to date" true rather than aspirational.
 *  2. Show download state on the toolbar icon.
 *  3. Notify when a download finishes or fails, and open the file on click.
 *  4. Offer a right-click "Download with yt-dlp" on links, pages and media.
 */

const SERVER = "http://127.0.0.1:4599";

// How often to ask the server whether a newer extension is on disk. Chrome
// will not fire an alarm more than once a minute, so this is also the floor on
// how fast a download-progress badge can update while the worker is idle.
const CHECK_ALARM = "ytdlp-check";
const CHECK_PERIOD_MINUTES = 1;

// While something is downloading the badge needs to move faster than an alarm
// can fire, so poll directly. Each fetch resets the worker's idle timer, which
// keeps it alive for exactly as long as there is something to show.
const ACTIVE_POLL_MS = 2000;

const RUNNING_VERSION = chrome.runtime.getManifest().version;

// --------------------------------------------------------------------------- //
// server access
// --------------------------------------------------------------------------- //

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
// self-update
// --------------------------------------------------------------------------- //

/** Compare dotted versions numerically ("0.10.0" is newer than "0.9.0"). */
function isNewer(candidate, current) {
  const a = String(candidate || "").split(".").map((n) => parseInt(n, 10) || 0);
  const b = String(current || "").split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0);
  }
  return false;
}

/** Is one of our own pages open? Reloading would close it out from under the user. */
async function hasOpenExtensionPage() {
  try {
    const contexts = await chrome.runtime.getContexts({});
    return contexts.some((c) => c.contextType === "TAB" || c.contextType === "POPUP");
  } catch (e) {
    return false;   // getContexts needs Chrome 116; assume nothing is open
  }
}

async function checkForExtensionUpdate() {
  let info;
  try {
    info = await api("/extension");
  } catch (e) {
    return;   // server down: nothing to compare against, and nothing to do
  }

  const onDisk = info.version;
  if (!onDisk || !isNewer(onDisk, RUNNING_VERSION)) {
    await chrome.storage.local.remove("updateReady");
    return;
  }

  // Reloading once per on-disk version, and only once, is the whole guard
  // against a reload loop: if the reload doesn't take -- a syntax error in the
  // new files, a manifest Chrome rejects -- the old worker comes back, sees the
  // same version it already tried, and stops instead of thrashing the browser.
  const { reloadedFor } = await chrome.storage.local.get("reloadedFor");
  if (reloadedFor === onDisk) return;

  if (await hasOpenExtensionPage()) {
    // The dashboard picks this up and offers a Reload button, so an update
    // never yanks a page the user is looking at.
    await chrome.storage.local.set({ updateReady: onDisk });
    return;
  }

  await chrome.storage.local.set({ reloadedFor: onDisk, announceVersion: onDisk });
  await chrome.storage.local.remove("updateReady");
  chrome.runtime.reload();
}

/** Say what happened, once, after a reload we asked for. */
async function announceUpdateIfJustReloaded() {
  const { announceVersion } = await chrome.storage.local.get("announceVersion");
  if (!announceVersion) return;
  await chrome.storage.local.remove("announceVersion");
  if (announceVersion !== RUNNING_VERSION) return;   // the reload didn't take
  notify(`ytdlp-updated-${RUNNING_VERSION}`, "Extension updated",
         `Now running v${RUNNING_VERSION}, matching the helper server.`);
}

// --------------------------------------------------------------------------- //
// notifications
// --------------------------------------------------------------------------- //

function notify(id, title, message) {
  try {
    chrome.notifications.create(id, {
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon128.png"),
      title,
      message: message.slice(0, 250),
    });
  } catch (e) {
    // Notifications can be switched off at the OS level; never let that break
    // the poll loop that called us.
  }
}

// Clicking a "download finished" notification should open the file, which is
// the only reason anyone clicks one. The notification id carries the record id.
chrome.notifications.onClicked.addListener(async (id) => {
  if (!id.startsWith("ytdlp-done-")) return;
  try {
    await postJson("/open_file", { id: id.slice("ytdlp-done-".length) });
  } catch (e) {
    /* the file was moved or deleted; nothing useful to say */
  }
  chrome.notifications.clear(id);
});

// --------------------------------------------------------------------------- //
// badge + finish notifications
// --------------------------------------------------------------------------- //

// Which records we have already reported on. Kept in session storage rather
// than a module variable: the worker is torn down whenever it goes idle, and a
// plain variable would come back empty and re-announce every finished download.
async function seenStatuses() {
  const { seen } = await chrome.storage.session.get("seen");
  return seen || {};
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text: text || "" });
  if (text) chrome.action.setBadgeBackgroundColor({ color: color || "#2563eb" });
}

let activePollTimer = null;

async function refreshState() {
  let history;
  try {
    history = await api("/history");
  } catch (e) {
    setBadge("!", "#dc2626");
    chrome.action.setTitle({ title: "yt-dlp Downloader — helper server not running" });
    return false;
  }

  const items = history.items || [];
  const active = items.filter((i) => ["queued", "downloading", "processing"].includes(i.status));

  if (active.length) {
    // One download: show its percentage. Several: show how many.
    const pct = active.length === 1 ? active[0].percent_value : null;
    setBadge(active.length === 1 && pct != null ? `${Math.round(pct)}` : String(active.length));
    chrome.action.setTitle({
      title: active.length === 1
        ? `Downloading: ${active[0].title || ""} ${active[0].percent || ""}`.trim()
        : `${active.length} downloads in progress`,
    });
  } else {
    setBadge("");
    chrome.action.setTitle({ title: "yt-dlp Downloader" });
  }

  // Announce transitions into a finished state, once each.
  const seen = await seenStatuses();
  const next = {};
  for (const item of items.slice(0, 60)) {
    next[item.id] = item.status;
    const before = seen[item.id];
    if (!before || before === item.status) continue;
    if (item.status === "done") {
      notify(`ytdlp-done-${item.id}`, "Download finished",
             `${item.title || "Video"}${item.filesize_human ? ` · ${item.filesize_human}` : ""}`);
    } else if (item.status === "error") {
      notify(`ytdlp-error-${item.id}`, "Download failed",
             `${item.title || "Video"}: ${item.error || "unknown error"}`);
    }
  }
  await chrome.storage.session.set({ seen: next });

  return active.length > 0;
}

/** Poll fast while anything is running, then fall back to the alarm. */
async function pollWhileActive() {
  clearTimeout(activePollTimer);
  const stillActive = await refreshState();
  if (stillActive) {
    activePollTimer = setTimeout(pollWhileActive, ACTIVE_POLL_MS);
  }
}

// --------------------------------------------------------------------------- //
// context menus
// --------------------------------------------------------------------------- //

const MENU_PARENT = "ytdlp-root";
const MENU_DEFAULT = "ytdlp-default";
const MENU_QUALITIES = [
  ["max", "Best quality"],
  ["mp4", "Best MP4 (most compatible)"],
  ["1080", "Up to 1080p"],
  ["720", "Up to 720p"],
  ["mp3", "Audio only (MP3)"],
];

function createMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_PARENT,
      title: "Download with yt-dlp",
      contexts: ["link", "page", "video", "audio", "selection"],
    });
    chrome.contextMenus.create({
      id: MENU_DEFAULT,
      parentId: MENU_PARENT,
      title: "Use my usual quality",
      contexts: ["link", "page", "video", "audio", "selection"],
    });
    chrome.contextMenus.create({
      id: "ytdlp-sep",
      parentId: MENU_PARENT,
      type: "separator",
      contexts: ["link", "page", "video", "audio", "selection"],
    });
    for (const [key, label] of MENU_QUALITIES) {
      chrome.contextMenus.create({
        id: `ytdlp-q-${key}`,
        parentId: MENU_PARENT,
        title: label,
        contexts: ["link", "page", "video", "audio", "selection"],
      });
    }
  });
}

/** The URL the click was actually about, most specific first. */
function targetUrl(info) {
  const candidates = [info.linkUrl, info.srcUrl, info.pageUrl];
  for (const url of candidates) {
    if (url && /^https?:\/\//.test(url)) return url;
  }
  return null;
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!info.menuItemId.startsWith("ytdlp-")) return;
  const url = targetUrl(info);
  if (!url) {
    notify("ytdlp-nolink", "Nothing to download", "That isn't an http(s) URL.");
    return;
  }

  let quality;
  if (info.menuItemId === MENU_DEFAULT) {
    // The popup stores its choice as "preset:max" or "format:137"; an exact
    // format id belongs to one specific video, so it can't be reused here.
    const { quality: stored } = await chrome.storage.local.get("quality");
    quality = stored && stored.startsWith("preset:") ? stored.slice(7) : "max";
  } else {
    quality = info.menuItemId.replace("ytdlp-q-", "");
  }

  const prefs = await chrome.storage.local.get(
    ["embed_thumbnail", "subtitles", "embed_metadata"]);

  try {
    await postJson("/download", {
      url,
      quality,
      title: tab && tab.title ? tab.title : url,
      embed_thumbnail: prefs.embed_thumbnail ?? false,
      subtitles: prefs.subtitles ?? false,
      embed_metadata: prefs.embed_metadata ?? true,
    });
    notify(`ytdlp-start-${Date.now()}`, "Download started", url);
    pollWhileActive();
  } catch (e) {
    notify(`ytdlp-failed-${Date.now()}`, "Could not start the download", e.message);
  }
});

// --------------------------------------------------------------------------- //
// wiring
// --------------------------------------------------------------------------- //

function ensureAlarm() {
  chrome.alarms.create(CHECK_ALARM, { periodInMinutes: CHECK_PERIOD_MINUTES });
}

let lastTick = 0;

async function tick() {
  // A sleeping worker is woken *by* the alarm, so it runs its top-level tick
  // and then the alarm listener's -- two full polls for one wake-up. Anything
  // that close together is the same tick.
  const now = Date.now();
  if (now - lastTick < 500) return;
  lastTick = now;

  await refreshState();
  await checkForExtensionUpdate();
}

chrome.runtime.onInstalled.addListener(() => {
  createMenus();
  ensureAlarm();
  announceUpdateIfJustReloaded();
  tick();
});

chrome.runtime.onStartup.addListener(() => {
  createMenus();
  ensureAlarm();
  tick();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === CHECK_ALARM) tick();
});

// A message from the popup means a download just started, so start watching
// immediately rather than waiting up to a minute for the next alarm.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "download-started") {
    pollWhileActive();
    sendResponse({ ok: true });
  } else if (msg && msg.type === "reload-extension") {
    chrome.storage.local.set({ reloadedFor: msg.version || "" }, () => chrome.runtime.reload());
    sendResponse({ ok: true });
  }
  return true;
});

// The worker is also started cold by any of the events above; run once on load
// so a fresh start reports the right badge without waiting for an alarm.
ensureAlarm();
announceUpdateIfJustReloaded();
tick();
