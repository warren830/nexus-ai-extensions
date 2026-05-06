// background/session_manager.js
//
// Wraps chrome.storage.session for session → tab mapping + per-session
// authorization state (spec §5 Session → Agent Tab Mapping, §8.2 会话级授权).
//
// chrome.storage.session survives SW GC (only cleared on browser restart),
// so we can recover mapping state after the SW is killed.
//
// Keys:
//   session_map       → { [sessionId]: tabId }
//   window_map        → { [sessionId]: windowId }  // agent-tab isolation
//   auth_map          → { [sessionId]: { granted_at: isoString } }
//
// All methods are async and idempotent.

const KEY_SESSION_MAP = 'session_map';
const KEY_WINDOW_MAP = 'window_map';
const KEY_AUTH_MAP = 'auth_map';

async function _read(key, fallback) {
  try {
    const result = await chrome.storage.session.get(key);
    return result[key] || fallback;
  } catch (e) {
    // chrome.storage.session unavailable in some test envs; return fallback.
    return fallback;
  }
}

async function _write(key, value) {
  try {
    await chrome.storage.session.set({ [key]: value });
  } catch (e) {
    // swallow; SW-state write errors are non-fatal for MVP.
  }
}

/* --- session → tab mapping --- */

export async function getTabForSession(sessionId) {
  const map = await _read(KEY_SESSION_MAP, {});
  return map[sessionId] || null;
}

export async function setTabForSession(sessionId, tabId) {
  const map = await _read(KEY_SESSION_MAP, {});
  map[sessionId] = tabId;
  await _write(KEY_SESSION_MAP, map);
}

export async function removeSession(sessionId) {
  const map = await _read(KEY_SESSION_MAP, {});
  const winMap = await _read(KEY_WINDOW_MAP, {});
  const authMap = await _read(KEY_AUTH_MAP, {});
  delete map[sessionId];
  delete winMap[sessionId];
  delete authMap[sessionId];
  await _write(KEY_SESSION_MAP, map);
  await _write(KEY_WINDOW_MAP, winMap);
  await _write(KEY_AUTH_MAP, authMap);
}

/* --- session → window mapping ---
 *
 * Each agent session lives in its own dedicated Chrome window so that
 * chrome.tabs.captureVisibleTab(windowId) always returns the correct
 * frame. In a shared window, Chrome's API captures the visible tab of
 * that window — i.e. whatever the user is focused on — not the tab we
 * care about, which is how "screenshot shows the chat UI" happened.
 *
 * Tab and window mappings are tracked in parallel (not nested) to keep
 * each single-purpose storage access atomic and to minimise the blast
 * radius if one map drifts.
 */

export async function getWindowForSession(sessionId) {
  const map = await _read(KEY_WINDOW_MAP, {});
  return map[sessionId] || null;
}

export async function setWindowForSession(sessionId, windowId) {
  const map = await _read(KEY_WINDOW_MAP, {});
  map[sessionId] = windowId;
  await _write(KEY_WINDOW_MAP, map);
}

export async function findSessionByWindow(windowId) {
  const map = await _read(KEY_WINDOW_MAP, {});
  for (const [sid, wid] of Object.entries(map)) {
    if (wid === windowId) return sid;
  }
  return null;
}

export async function findSessionByTab(tabId) {
  const map = await _read(KEY_SESSION_MAP, {});
  for (const [sid, tid] of Object.entries(map)) {
    if (tid === tabId) return sid;
  }
  return null;
}

export async function listActiveSessions() {
  const map = await _read(KEY_SESSION_MAP, {});
  return Object.entries(map).map(([sessionId, tabId]) => ({ sessionId, tabId }));
}

export async function activeSessionCount() {
  const sessions = await listActiveSessions();
  return sessions.length;
}

/* --- per-session authorization state (R3 / §8.2) --- */

export async function isAuthorized(sessionId) {
  const authMap = await _read(KEY_AUTH_MAP, {});
  return Boolean(authMap[sessionId]);
}

export async function grantAuthorization(sessionId) {
  const authMap = await _read(KEY_AUTH_MAP, {});
  authMap[sessionId] = { granted_at: new Date().toISOString() };
  await _write(KEY_AUTH_MAP, authMap);
}

export async function revokeAuthorization(sessionId) {
  const authMap = await _read(KEY_AUTH_MAP, {});
  delete authMap[sessionId];
  await _write(KEY_AUTH_MAP, authMap);
}

/* --- recovery after SW restart --- */

/**
 * Validate each mapped tab still exists; drop dead ones.
 * Call on SW startup.
 * @returns {Promise<string[]>} deletedSessionIds
 */
export async function reconcileTabs() {
  const map = await _read(KEY_SESSION_MAP, {});
  const winMap = await _read(KEY_WINDOW_MAP, {});
  const authMap = await _read(KEY_AUTH_MAP, {});
  const dead = [];

  for (const [sid, tid] of Object.entries(map)) {
    let alive = false;
    try {
      await chrome.tabs.get(tid);
      alive = true;
    } catch (e) {
      alive = false;
    }
    if (!alive) {
      delete map[sid];
      delete winMap[sid];
      delete authMap[sid];
      dead.push(sid);
    }
  }

  await _write(KEY_SESSION_MAP, map);
  await _write(KEY_WINDOW_MAP, winMap);
  await _write(KEY_AUTH_MAP, authMap);
  return dead;
}

export default {
  getTabForSession,
  setTabForSession,
  getWindowForSession,
  setWindowForSession,
  findSessionByWindow,
  removeSession,
  findSessionByTab,
  listActiveSessions,
  activeSessionCount,
  isAuthorized,
  grantAuthorization,
  revokeAuthorization,
  reconcileTabs,
};
