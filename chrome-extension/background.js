// When a claude-reports page opens and another tab already shows the same report, close the
// new tab, bring the old one (and its window) forward, and reload it so it shows the latest
// reply. host_permissions cover only the report server, so Chrome reveals the URLs of those
// tabs and no others; this script can't see or touch any other site.

const REPORT = /^http:\/\/(?:127\.0\.0\.1|localhost):47823\/r\/([A-Za-z0-9-]+)(?:[?#].*)?$/;
const PATTERNS = ["http://127.0.0.1:47823/r/*", "http://localhost:47823/r/*"];
const handled = new Set(); // tabs already dealt with: onCreated and onUpdated both fire

async function dedupe(tabId, url) {
  const m = REPORT.exec(url || "");
  if (!m || handled.has(tabId)) return;
  handled.add(tabId);
  setTimeout(() => handled.delete(tabId), 5000);
  const tabs = await chrome.tabs.query({ url: PATTERNS });
  const same = tabs
    .filter((t) => t.id !== tabId && REPORT.exec(t.url || t.pendingUrl || "")?.[1] === m[1])
    .sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
  if (!same.length) return;
  const keep = same[0];
  try {
    // Close the new tab first: reloading the kept one fires onUpdated too, and the new tab
    // must be gone by then so it can't be mistaken for the duplicate.
    await chrome.tabs.remove(tabId);
  } catch (_) {
    return; // already closed
  }
  handled.add(keep.id);
  setTimeout(() => handled.delete(keep.id), 5000);
  await chrome.tabs.update(keep.id, { active: true });
  await chrome.windows.update(keep.windowId, { focused: true });
  await chrome.tabs.reload(keep.id);
}

chrome.tabs.onCreated.addListener((tab) => dedupe(tab.id, tab.pendingUrl || tab.url));
chrome.tabs.onUpdated.addListener((tabId, info) => { if (info.url) dedupe(tabId, info.url); });
