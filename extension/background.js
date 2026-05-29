const STATE_URL = "http://localhost:7777/state";
const CACHE_MAX_AGE_MS = 10000;

let cachedState = { active: false, task: "", allowlist: [], end_time: 0 };
let cacheTimestamp = 0;
let inFlight = null;

async function refreshState() {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    try {
      const res = await fetch(STATE_URL, { cache: "no-store" });
      if (res.ok) {
        cachedState = await res.json();
        cacheTimestamp = Date.now();
      }
    } catch (_) {
      // server down — keep last known state
    } finally {
      inFlight = null;
    }
  })();
  return inFlight;
}

async function getState() {
  if (Date.now() - cacheTimestamp >= CACHE_MAX_AGE_MS) {
    await refreshState();
  }
  return cachedState;
}

function isAllowed(url, state) {
  if (!state.active) return true;
  if (Date.now() / 1000 >= state.end_time) return true;
  let u;
  try {
    u = new URL(url);
  } catch (_) {
    return true;
  }
  const skipProtocols = ["chrome-extension:", "chrome:", "edge:", "about:", "file:", "devtools:", "view-source:"];
  if (skipProtocols.includes(u.protocol)) return true;

  const host = u.hostname.toLowerCase();
  for (const raw of (state.allowlist || [])) {
    const d = String(raw).toLowerCase().replace(/^www\./, "").trim();
    if (!d) continue;
    if (host === d || host.endsWith("." + d)) return true;
  }
  return false;
}

async function maybeBlock(tabId, url) {
  if (!url || !/^https?:/i.test(url)) return;
  const state = await getState();
  if (isAllowed(url, state)) return;
  const blockedUrl = chrome.runtime.getURL("blocked.html") +
    "?url=" + encodeURIComponent(url) +
    "&task=" + encodeURIComponent(state.task || "") +
    "&end=" + encodeURIComponent(state.end_time || 0) +
    "&allowed=" + encodeURIComponent((state.allowlist || []).join(","));
  try {
    await chrome.tabs.update(tabId, { url: blockedUrl });
  } catch (_) {
    // tab may have closed
  }
}

chrome.webNavigation.onBeforeNavigate.addListener((d) => {
  if (d.frameId !== 0) return;
  maybeBlock(d.tabId, d.url);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.url) maybeBlock(tabId, changeInfo.url);
});

chrome.tabs.onCreated.addListener((tab) => {
  const url = tab.url || tab.pendingUrl;
  if (url) maybeBlock(tab.id, url);
});

chrome.alarms.create("refresh", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "refresh") refreshState();
});

refreshState();
