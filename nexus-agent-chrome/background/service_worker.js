// background/service_worker.js
//
// MV3 Service Worker entry point for the Nexus Agent extension.
//
// Responsibilities (spec §5 Service Worker 职责):
//   - WSS client: connect, handshake (extension.online → extension.registered),
//     heartbeat, exponential backoff reconnect.
//   - Message dispatch: browser.navigate / act / observe / human.request /
//     agent.pause from server → content script → browser.result back.
//   - Toolbar icon state machine (badge_manager).
//   - Session → tab mapping (session_manager) + authorization state.
//   - Notifications for initial authorization, wait_for_human, token errors.
//   - Keyboard shortcut Alt+Shift+P → pause all sessions.
//   - Concurrency cap: ≤ 5 agent tabs per extension.
//   - Protocol version 1.0; MAJOR mismatch triggers close code 4004.

import { STATES, setState as setBadgeState } from './badge_manager.js';
import sessionMgr from './session_manager.js';

// 1.1: adds optional `device_id` to extension.online frame
//      + Web UI externally_connectable path (nexus.setup / nexus.ping /
//        nexus.refresh). MINOR bump — server still accepts 1.0 clients.
const PROTOCOL_VERSION = '1.1';
const EXTENSION_VERSION = '0.1.0';
const MAX_AGENT_TABS = 5;
const MAX_RECONNECT_ATTEMPTS = 5;
const BACKOFF_CAP_MS = 30000;
const KEEPALIVE_ALARM = 'keepalive';
const KEEPALIVE_PERIOD_MIN = 0.4;      // ~24 s
// Wave 2: automatic token refresh.
// chrome.alarms fires every 30 min, we check whether the stored token is
// within 1 h of expiry and, if so, call /token/refresh. The constant is
// NOT the refresh cadence per se — it's the poll cadence for deciding
// whether to refresh. Actual refresh happens at most once per token
// lifetime (with the backend's 5-min rate limit as the floor).
const REFRESH_ALARM = 'token-refresh';
const REFRESH_POLL_PERIOD_MIN = 30;    // check every 30 min
const REFRESH_THRESHOLD_SECONDS = 3600; // refresh when <1h to expiry
const PENDING_REQUEST_TIMEOUT_MS = 30000;

/* ------------------------------------------------------------------ *
 * Module-scope state (ephemeral — recreated whenever SW restarts).   *
 * ------------------------------------------------------------------ */

let _ws = null;
let _wsState = 'disconnected';     // 'disconnected' | 'connecting' | 'open'
let _connectAttempts = 0;
let _nextBackoffMs = 1000;
let _alertReason = '';
// (Removed _pendingRequests — content-script RPC timeouts live inside
// sendToContentScript() closures, not a module-level map.)
const _notifPending = new Map();        // notification_id → { sessionId, requestId, kind }

/* ------------------------------------------------------------------ *
 * Config loading                                                     *
 * ------------------------------------------------------------------ */

async function loadConfig() {
  const result = await chrome.storage.local.get([
    'server_url', 'token', 'user_id', 'device_id',
  ]);
  return {
    serverUrl: (result.server_url || '').trim(),
    token: (result.token || '').trim(),
    userId: (result.user_id || '').trim(),
    deviceId: (result.device_id || '').trim() || null,
  };
}

function buildWsUrl(serverUrl, token) {
  // server_url should point at the Bridge service (default port 8001 in
  // dev, typically reverse-proxied behind a public host in prod). Accepts
  // http(s)://... or bare host; normalizes to ws(s):// and appends the
  // browser WebSocket endpoint.
  //
  // IMPORTANT: the Bridge router is mounted at /bridge/* (see
  // nexus_utils/bridge/main.py). There is no /api/v2 prefix for Bridge
  // traffic. See docs/browser-extension-install.md for setup guidance.
  let base = serverUrl.replace(/\/+$/, '');
  base = base.replace(/^http:\/\//i, 'ws://').replace(/^https:\/\//i, 'wss://');
  if (!/^wss?:\/\//i.test(base)) {
    base = 'wss://' + base;
  }
  return `${base}/bridge/browser/ws?token=${encodeURIComponent(token)}`;
}

/* ------------------------------------------------------------------ *
 * Status update helper                                               *
 * ------------------------------------------------------------------ */

async function refreshBadge() {
  const count = await sessionMgr.activeSessionCount();
  if (_alertReason) {
    setBadgeState(STATES.ALERT, { alertReason: _alertReason, version: EXTENSION_VERSION });
    return;
  }
  if (_wsState === 'disconnected') {
    setBadgeState(STATES.OFFLINE, { version: EXTENSION_VERSION });
    return;
  }
  if (_wsState === 'connecting') {
    setBadgeState(STATES.CONNECTING, { version: EXTENSION_VERSION });
    return;
  }
  if (count > 0) {
    setBadgeState(STATES.ACTIVE, { sessionCount: count, version: EXTENSION_VERSION });
  } else {
    setBadgeState(STATES.IDLE, { version: EXTENSION_VERSION });
  }
}

/* ------------------------------------------------------------------ *
 * WSS lifecycle                                                      *
 * ------------------------------------------------------------------ */

async function connect() {
  // Guard: idempotent — if already connecting/open, bail out.
  if (_wsState === 'open' || _wsState === 'connecting') return;

  const { serverUrl, token, userId, deviceId } = await loadConfig();
  if (!serverUrl || !token || !userId) {
    _wsState = 'disconnected';
    if (!userId && (serverUrl || token)) {
      _alertReason = 'User ID not configured. Open options to set it.';
    }
    await refreshBadge();
    return;
  }

  let url;
  try {
    url = buildWsUrl(serverUrl, token);
  } catch (e) {
    _wsState = 'disconnected';
    _alertReason = 'invalid server URL';
    await refreshBadge();
    return;
  }

  _wsState = 'connecting';
  _alertReason = '';
  await refreshBadge();

  try {
    _ws = new WebSocket(url);
  } catch (e) {
    _wsState = 'disconnected';
    scheduleReconnect();
    return;
  }

  // Capture userId now so the 'open' handler doesn't need async work.
  const helloPayload = {
    type: 'extension.online',
    user_id: userId,
    version: EXTENSION_VERSION,
    protocol_version: PROTOCOL_VERSION,
    chrome_version: (navigator?.userAgent?.match(/Chrome\/(\S+)/) || [, ''])[1] || '',
  };
  // device_id (product-UX) is optional — only present when this
  // extension was set up via the Web UI "Connect" flow. Legacy manual
  // paste setup leaves it null, and Bridge falls back to user-only
  // routing per the compat matrix (design §14c).
  if (deviceId) {
    helloPayload.device_id = deviceId;
  }

  _ws.addEventListener('open', () => {
    try {
      _ws.send(JSON.stringify(helloPayload));
    } catch (e) {
      // If send fails, the close handler will schedule a reconnect.
    }
  });

  _ws.addEventListener('message', async (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (e) {
      return;
    }
    await handleServerMessage(msg);
  });

  _ws.addEventListener('close', async (ev) => {
    const wasOpen = _wsState === 'open';
    _wsState = 'disconnected';
    _ws = null;

    // Close code 4004 = protocol MAJOR mismatch
    if (ev.code === 4004) {
      _alertReason = 'Protocol version incompatible. Please upgrade extension.';
      notifyProtocolMismatch();
      await refreshBadge();
      return;
    }
    // 4001 = invalid token, 4003 = user_id mismatch
    if (ev.code === 4001 || ev.code === 4003) {
      _alertReason = 'Token invalid or expired. Please reauthorize.';
      notifyTokenProblem();
      await refreshBadge();
      return;
    }

    await refreshBadge();
    if (wasOpen || _connectAttempts < MAX_RECONNECT_ATTEMPTS) {
      scheduleReconnect();
    } else {
      _alertReason = 'Unable to reach Nexus server after multiple attempts.';
      await refreshBadge();
    }
  });

  _ws.addEventListener('error', () => {
    // Error is almost always followed by close; let close handler reconnect.
  });
}

function scheduleReconnect() {
  _connectAttempts += 1;
  if (_connectAttempts > MAX_RECONNECT_ATTEMPTS) return;
  const delay = Math.min(_nextBackoffMs, BACKOFF_CAP_MS);
  _nextBackoffMs = Math.min(_nextBackoffMs * 2, BACKOFF_CAP_MS);
  setTimeout(() => {
    connect().catch(() => {});
  }, delay);
}

function resetBackoff() {
  _connectAttempts = 0;
  _nextBackoffMs = 1000;
}

function sendToServer(payload) {
  if (!_ws || _wsState !== 'open') return false;
  try {
    _ws.send(JSON.stringify(payload));
    return true;
  } catch (e) {
    return false;
  }
}

/* ------------------------------------------------------------------ *
 * Server message dispatch                                            *
 * ------------------------------------------------------------------ */

async function handleServerMessage(msg) {
  const type = msg.type || '';

  if (type === 'extension.registered') {
    _wsState = 'open';
    resetBackoff();
    _alertReason = '';
    await refreshBadge();
    maybeFirstConnectNotification();
    return;
  }

  if (type === 'heartbeat_ack') {
    // nothing to do
    return;
  }

  if (type === 'browser.navigate') {
    await handleBrowserNavigate(msg);
    return;
  }
  if (type === 'browser.act') {
    await handleBrowserAct(msg);
    return;
  }
  if (type === 'browser.observe') {
    await handleBrowserObserve(msg);
    return;
  }
  if (type === 'human.request') {
    await handleHumanRequest(msg);
    return;
  }
  if (type === 'agent.pause') {
    // MVP no-op: extension has no long-running task to actually pause.
    sendToServer({
      type: 'browser.result',
      session_id: msg.session_id,
      request_id: msg.request_id,
      ok: true,
      data: { acknowledged: true },
    });
    return;
  }

  // Unknown type — log but do not crash.
  console.warn('[Nexus] unknown server message type', type);
}

/* ------------------------------------------------------------------ *
 * browser.navigate — open or reuse Agent Tab                         *
 * ------------------------------------------------------------------ */

async function handleBrowserNavigate(msg) {
  const { session_id, request_id, params = {} } = msg;
  const { url, title_summary = '' } = params;

  // Concurrency cap
  const existing = await sessionMgr.getTabForSession(session_id);
  if (!existing) {
    const count = await sessionMgr.activeSessionCount();
    if (count >= MAX_AGENT_TABS) {
      notifyMaxSessions();
      sendToServer({
        type: 'browser.result',
        session_id,
        request_id,
        ok: false,
        error: 'max_sessions_exceeded',
      });
      return;
    }
  }

  try {
    let tabId;
    if (existing) {
      await chrome.tabs.update(existing, { url, active: false });
      tabId = existing;
    } else {
      const tab = await chrome.tabs.create({ url, active: false });
      tabId = tab.id;
      await sessionMgr.setTabForSession(session_id, tabId);
    }

    // Wait briefly for load, then set title + inject content script.
    await waitForTabComplete(tabId, 10000).catch(() => {});
    const titleText = title_summary
      ? `[\u{1F916} ${title_summary}]`
      : `[\u{1F916} Nexus]`;
    // chrome.tabs has no setTitle; inject a script that rewrites document.title.
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: (t) => { document.title = t; },
        args: [titleText],
      });
    } catch (e) { /* ignore */ }

    await injectContentScript(tabId);

    // Read final URL / title from the tab.
    let finalUrl = url;
    let finalTitle = titleText;
    try {
      const t = await chrome.tabs.get(tabId);
      finalUrl = t.url || url;
    } catch (e) { /* ignore */ }

    await refreshBadge();

    sendToServer({
      type: 'browser.result',
      session_id,
      request_id,
      ok: true,
      data: { url: finalUrl, title: finalTitle },
    });
  } catch (e) {
    sendToServer({
      type: 'browser.result',
      session_id,
      request_id,
      ok: false,
      error: 'navigate_failed',
      error_detail: String(e?.message || e),
    });
  }
}

async function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const check = async () => {
      try {
        const t = await chrome.tabs.get(tabId);
        if (t.status === 'complete') return resolve();
      } catch (e) {
        return reject(e);
      }
      if (Date.now() - start > timeoutMs) return resolve();  // best-effort
      setTimeout(check, 200);
    };
    check();
  });
}

async function injectContentScript(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['content/content.js'],
    });
  } catch (e) {
    // Might fail on chrome:// pages or during navigation; swallow.
  }
}

/* ------------------------------------------------------------------ *
 * browser.act — authorization + delegate to content script           *
 * ------------------------------------------------------------------ */

async function handleBrowserAct(msg) {
  const { session_id, request_id, params = {} } = msg;
  const { instruction, task_description, title_summary } = params;

  const tabId = await sessionMgr.getTabForSession(session_id);
  if (!tabId) {
    sendToServer({
      type: 'browser.result',
      session_id,
      request_id,
      ok: false,
      error: 'no_agent_tab',
      error_detail: 'browser.act called before browser.navigate for this session',
    });
    return;
  }

  const alreadyAuthorized = await sessionMgr.isAuthorized(session_id);

  if (!alreadyAuthorized) {
    // First browser.act for this session — pop authorization notification.
    let currentUrl = '';
    try {
      const t = await chrome.tabs.get(tabId);
      currentUrl = (t.url || '').replace(/^https?:\/\//, '').split('/')[0];
    } catch (e) { /* ignore */ }

    const granted = await requestAuthorization(session_id, task_description || instruction, currentUrl);
    if (!granted) {
      sendToServer({
        type: 'browser.result',
        session_id,
        request_id,
        ok: false,
        error: 'authorization_denied',
        error_detail: 'User denied or timed out initial authorization',
      });
      return;
    }
    await sessionMgr.grantAuthorization(session_id);
  }

  // Update tab title if new summary provided.
  if (title_summary) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        func: (t) => { document.title = t; },
        args: [`[\u{1F916} ${title_summary}]`],
      });
    } catch (e) { /* ignore */ }
  }

  // Ask content script to execute the action.
  try {
    const result = await sendToContentScript(tabId, {
      action: 'act',
      instruction,
    });
    let newUrl = null;
    try {
      const t = await chrome.tabs.get(tabId);
      newUrl = t.url || null;
    } catch (e) { /* ignore */ }
    sendToServer({
      type: 'browser.result',
      session_id,
      request_id,
      ok: Boolean(result?.ok),
      data: {
        action_taken: result?.action_taken || 'unknown',
        target: result?.target || '',
        new_url: newUrl,
      },
      ...(result?.ok ? {} : { error: result?.error || 'act_failed' }),
    });
  } catch (e) {
    sendToServer({
      type: 'browser.result',
      session_id,
      request_id,
      ok: false,
      error: 'content_script_error',
      error_detail: String(e?.message || e),
    });
  }
}

/* ------------------------------------------------------------------ *
 * browser.observe — ax_tree + optional screenshot                    *
 * ------------------------------------------------------------------ */

async function handleBrowserObserve(msg) {
  const { session_id, request_id, params = {} } = msg;
  const { query = null, use_vision = false } = params;

  const tabId = await sessionMgr.getTabForSession(session_id);
  if (!tabId) {
    sendToServer({
      type: 'browser.result',
      session_id,
      request_id,
      ok: false,
      error: 'no_agent_tab',
    });
    return;
  }

  try {
    const result = await sendToContentScript(tabId, {
      action: 'observe',
      query,
    });

    let screenshotB64 = null;
    if (use_vision) {
      try {
        const tab = await chrome.tabs.get(tabId);
        if (tab.windowId != null) {
          const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
            format: 'jpeg',
            quality: 70,
          });
          screenshotB64 = dataUrl?.replace(/^data:image\/[a-z]+;base64,/i, '') || null;
        }
      } catch (e) { /* ignore */ }
    }

    sendToServer({
      type: 'browser.result',
      session_id,
      request_id,
      ok: true,
      data: {
        ax_tree: result?.ax_tree || [],
        url: result?.url || '',
        title: result?.title || '',
        screenshot_b64: screenshotB64,
        truncated: Boolean(result?.truncated),
      },
    });
  } catch (e) {
    sendToServer({
      type: 'browser.result',
      session_id,
      request_id,
      ok: false,
      error: 'observe_failed',
      error_detail: String(e?.message || e),
    });
  }
}

/* ------------------------------------------------------------------ *
 * human.request — notification with screenshot                       *
 * ------------------------------------------------------------------ */

async function handleHumanRequest(msg) {
  const { session_id, request_id, params = {} } = msg;
  const { reason = 'Agent needs your help', suggestion = '', timeout = 300 } = params;

  const tabId = await sessionMgr.getTabForSession(session_id);
  let imageUrl = null;
  if (tabId != null) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab.windowId != null) {
        imageUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
          format: 'jpeg',
          quality: 60,
        });
      }
    } catch (e) { /* ignore */ }
  }

  const notifOptions = {
    type: imageUrl ? 'image' : 'basic',
    iconUrl: chrome.runtime.getURL('icons/icon-alert-128.png'),
    title: reason,
    message: suggestion || 'Switch to the Agent Tab to complete the action.',
    buttons: [
      { title: 'Done' },
      { title: 'Cancel task' },
    ],
    priority: 2,
    requireInteraction: true,
  };
  if (imageUrl) notifOptions.imageUrl = imageUrl;

  const notifId = `nexus-human-${request_id}`;
  try {
    chrome.notifications.create(notifId, notifOptions);
  } catch (e) {
    // Fallback to basic if image type rejected (some platforms).
    chrome.notifications.create(notifId, { ...notifOptions, type: 'basic', imageUrl: undefined });
  }
  _notifPending.set(notifId, { sessionId: session_id, requestId: request_id, kind: 'human' });

  // Timeout → auto-cancel and reply
  setTimeout(() => {
    if (_notifPending.has(notifId)) {
      _notifPending.delete(notifId);
      try { chrome.notifications.clear(notifId); } catch (e) { /* ignore */ }
      sendToServer({
        type: 'human.response',
        session_id,
        request_id,
        response: 'timeout',
        note: null,
      });
    }
  }, Math.max(10, timeout) * 1000);

  _alertReason = '';  // human.request → ALERT red helps draw attention
  setBadgeState(STATES.ALERT, { alertReason: reason, version: EXTENSION_VERSION });
}

/* ------------------------------------------------------------------ *
 * Authorization prompt (notification-based)                          *
 * ------------------------------------------------------------------ */

function requestAuthorization(sessionId, taskDescription, domain) {
  return new Promise((resolve) => {
    const notifId = `nexus-auth-${sessionId}-${Date.now()}`;
    const message = `${taskDescription || 'Agent wants to perform a task'}${domain ? `\nOn: ${domain}` : ''}`;
    try {
      chrome.notifications.create(notifId, {
        type: 'basic',
        iconUrl: chrome.runtime.getURL('icons/icon-128.png'),
        title: 'Agent requests authorization',
        message,
        buttons: [{ title: 'Authorize' }, { title: 'Deny' }],
        priority: 2,
        requireInteraction: true,
      });
    } catch (e) {
      return resolve(false);
    }

    _notifPending.set(notifId, {
      sessionId,
      kind: 'auth',
      resolve,
    });

    // Timeout 60 s — treated as deny.
    setTimeout(() => {
      if (_notifPending.has(notifId)) {
        _notifPending.delete(notifId);
        try { chrome.notifications.clear(notifId); } catch (e) { /* ignore */ }
        resolve(false);
      }
    }, 60000);
  });
}

/* ------------------------------------------------------------------ *
 * Content script RPC                                                 *
 * ------------------------------------------------------------------ */

function sendToContentScript(tabId, message) {
  return new Promise((resolve, reject) => {
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      reject(new Error('content_script_timeout'));
    }, PENDING_REQUEST_TIMEOUT_MS);

    try {
      chrome.tabs.sendMessage(tabId, message, (response) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        const err = chrome.runtime.lastError;
        if (err) {
          reject(new Error(err.message || 'runtime_error'));
        } else {
          resolve(response || {});
        }
      });
    } catch (e) {
      if (!done) {
        done = true;
        clearTimeout(timer);
        reject(e);
      }
    }
  });
}

/* ------------------------------------------------------------------ *
 * Notification helpers                                               *
 * ------------------------------------------------------------------ */

let _welcomeShown = false;
function maybeFirstConnectNotification() {
  if (_welcomeShown) return;
  _welcomeShown = true;
  try {
    chrome.notifications.create('nexus-ready', {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/icon-128.png'),
      title: 'Nexus Agent',
      message: 'Nexus Agent is ready.',
      priority: 0,
    });
  } catch (e) { /* ignore */ }
}

function notifyProtocolMismatch() {
  try {
    chrome.notifications.create('nexus-proto-mismatch', {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/icon-alert-128.png'),
      title: 'Nexus Agent: please upgrade',
      message: 'Protocol version incompatible with server. Please download the latest extension.',
      buttons: [{ title: 'Open options' }],
      priority: 2,
    });
  } catch (e) { /* ignore */ }
}

function notifyTokenProblem() {
  try {
    chrome.notifications.create('nexus-token-problem', {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/icon-alert-128.png'),
      title: 'Nexus Agent: authorization failed',
      message: 'Token invalid or expired. Please generate a new one in Nexus Web UI.',
      buttons: [{ title: 'Open options' }],
      priority: 2,
    });
  } catch (e) { /* ignore */ }
}

function notifyMaxSessions() {
  try {
    chrome.notifications.create(`nexus-max-${Date.now()}`, {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/icon-alert-128.png'),
      title: 'Nexus Agent: too many sessions',
      message: `Max ${MAX_AGENT_TABS} concurrent Agent Tabs. Please close an idle one and retry.`,
      priority: 1,
    });
  } catch (e) { /* ignore */ }
}

/* ------------------------------------------------------------------ *
 * Chrome event listeners                                             *
 * ------------------------------------------------------------------ */

/**
 * Ensure both the keepalive alarm and the token-refresh alarm exist.
 * Chrome will dedupe if they already exist (create is idempotent by
 * name). Called from onInstalled / onStartup / module-top IIFE to
 * cover all SW wake-up paths.
 */
function _ensureAlarms() {
  chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: KEEPALIVE_PERIOD_MIN });
  chrome.alarms.create(REFRESH_ALARM, { periodInMinutes: REFRESH_POLL_PERIOD_MIN });
}

chrome.runtime.onInstalled.addListener(async () => {
  await refreshBadge();
  await sessionMgr.reconcileTabs();
  _ensureAlarms();
  connect().catch(() => {});
});

chrome.runtime.onStartup.addListener(async () => {
  await refreshBadge();
  await sessionMgr.reconcileTabs();
  _ensureAlarms();
  connect().catch(() => {});
});

// Immediate connect on SW start (fires even outside install/startup events
// when Chrome wakes the worker back up).
(async () => {
  try {
    await refreshBadge();
    await sessionMgr.reconcileTabs();
    _ensureAlarms();
    await connect();
  } catch (e) { /* ignore */ }
})();

/**
 * Decode a browser token's exp claim. Returns null if the token is
 * malformed or unparseable (we then skip the refresh check — the WS
 * layer will surface the error). Never throws.
 *
 * Token format is `base64url(json_payload + '.' + b64_sig)` double-
 * encoded — see token_service._sign_payload. For exp we only need the
 * outer decode to get at the embedded JSON body; signature doesn't
 * matter here since we're just peeking, not verifying.
 */
function _parseTokenExp(token) {
  try {
    // `atob` doesn't handle URL-safe base64 directly.
    const base64 = token.replace(/-/g, '+').replace(/_/g, '/');
    const pad = base64.length % 4 === 0 ? '' : '='.repeat(4 - (base64.length % 4));
    const decoded = atob(base64 + pad);
    // Strip the `.<b64_sig>` tail: payload is json before first '.'
    const jsonPart = decoded.split('.')[0];
    const payload = JSON.parse(jsonPart);
    return typeof payload?.exp === 'number' ? payload.exp : null;
  } catch (_) {
    return null;
  }
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === KEEPALIVE_ALARM) {
    if (_wsState === 'open') {
      sendToServer({ type: 'heartbeat' });
    } else if (_wsState === 'disconnected') {
      connect().catch(() => {});
    }
    return;
  }

  if (alarm.name === REFRESH_ALARM) {
    // Wave 2: proactive token refresh. We only refresh when the stored
    // token is in its last hour AND the extension has a device_id
    // (legacy manual-paste tokens lack `did` — backend rejects
    // refresh for those per design §6.2).
    try {
      const { token, device_id } = await chrome.storage.local.get(
        ['token', 'device_id'],
      );
      if (!token || !device_id) return;
      const exp = _parseTokenExp(token);
      if (exp == null) return;
      const secondsLeft = exp - Math.floor(Date.now() / 1000);
      if (secondsLeft > REFRESH_THRESHOLD_SECONDS) return;
      const result = await refreshTokenNow();
      if (!result.ok) {
        console.warn('[Nexus] auto-refresh failed:', result);
        // Surface terminal failures (revoked / total TTL / legacy) so the
        // user knows to reconnect. Transient HTTP errors silently retry
        // on next alarm tick.
        const reason = result.detail?.reason || result.error;
        if (
          reason === 'device_revoked' ||
          reason === 'total_ttl_exceeded' ||
          reason === 'legacy_token_no_device'
        ) {
          _alertReason = `Token refresh failed (${reason}). Please reconnect via Web UI.`;
          await refreshBadge();
        }
      }
    } catch (e) {
      console.warn('[Nexus] alarm refresh handler threw:', e);
    }
  }
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const sid = await sessionMgr.findSessionByTab(tabId);
  if (!sid) return;
  await sessionMgr.removeSession(sid);
  sendToServer({ type: 'browser.tab_closed', session_id: sid });
  await refreshBadge();
});

chrome.notifications.onButtonClicked.addListener((notifId, buttonIndex) => {
  const pending = _notifPending.get(notifId);
  if (!pending) {
    // could be protocol/token notification — open options on button click
    if (notifId === 'nexus-proto-mismatch' || notifId === 'nexus-token-problem') {
      chrome.runtime.openOptionsPage();
    }
    return;
  }
  _notifPending.delete(notifId);
  try { chrome.notifications.clear(notifId); } catch (e) { /* ignore */ }

  if (pending.kind === 'auth') {
    pending.resolve(buttonIndex === 0);   // 0 = Authorize, 1 = Deny
    return;
  }
  if (pending.kind === 'human') {
    const response = buttonIndex === 0 ? 'done' : 'cancel';
    sendToServer({
      type: 'human.response',
      session_id: pending.sessionId,
      request_id: pending.requestId,
      response,
      note: null,
    });
    // Return icon to normal.
    _alertReason = '';
    refreshBadge().catch(() => {});
  }
});

chrome.notifications.onClosed.addListener((notifId, byUser) => {
  const pending = _notifPending.get(notifId);
  if (!pending) return;
  if (!byUser) return;   // auto-closed by browser; let timeout handler run.
  _notifPending.delete(notifId);

  if (pending.kind === 'auth') {
    pending.resolve(false);
  } else if (pending.kind === 'human') {
    sendToServer({
      type: 'human.response',
      session_id: pending.sessionId,
      request_id: pending.requestId,
      response: 'cancel',
      note: null,
    });
    _alertReason = '';
    refreshBadge().catch(() => {});
  }
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== 'pause-all-agents') return;
  const sessions = await sessionMgr.listActiveSessions();
  for (const { sessionId } of sessions) {
    sendToServer({ type: 'user.pause_requested', session_id: sessionId });
  }
  try {
    chrome.notifications.create(`nexus-pause-${Date.now()}`, {
      type: 'basic',
      iconUrl: chrome.runtime.getURL('icons/icon-128.png'),
      title: 'Nexus Agent: pause requested',
      message: `Paused ${sessions.length} session${sessions.length === 1 ? '' : 's'}.`,
      priority: 1,
    });
  } catch (e) { /* ignore */ }
});

chrome.action.onClicked.addListener(async () => {
  // Clicking the icon in OFFLINE or ALERT state → open options.
  if (_wsState !== 'open' || _alertReason) {
    chrome.runtime.openOptionsPage();
  }
});

// Accept messages from options page (config update, revoke, etc.)
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return false;

  if (msg.command === 'reconnect') {
    resetBackoff();
    try { _ws?.close(); } catch (e) { /* ignore */ }
    connect().then(() => sendResponse({ ok: true })).catch((e) =>
      sendResponse({ ok: false, error: String(e?.message || e) })
    );
    return true;
  }
  if (msg.command === 'get_status') {
    sessionMgr.listActiveSessions().then((sessions) => {
      sendResponse({
        ok: true,
        wsState: _wsState,
        alertReason: _alertReason,
        sessions,
        version: EXTENSION_VERSION,
        protocolVersion: PROTOCOL_VERSION,
      });
    });
    return true;
  }
  if (msg.command === 'revoke_session') {
    const { session_id } = msg;
    sessionMgr.removeSession(session_id).then(() => {
      sendToServer({ type: 'authorization.revoked', session_id });
      sendResponse({ ok: true });
    });
    return true;
  }
  return false;
});

/* ────────────────────────────────────────────────────────────
 * External messages — Web UI → extension (product-UX rework).
 * ────────────────────────────────────────────────────────────
 * When the user clicks "Connect Extension" in the Nexus Web UI,
 * the page sends us a `nexus.setup` message with a fresh token,
 * user_id, server_url, and device_id. We persist + reconnect.
 *
 * Chrome gates externally_connectable to the domains in manifest.json
 * (see `externally_connectable.matches`). Even so we double-check
 * sender.origin / sender.url to block cousin-tab attacks from scripts
 * running on whitelisted origins but under a different iframe chain.
 * ──────────────────────────────────────────────────────────── */

const ALLOWED_EXTERNAL_ORIGIN_RE = /^https?:\/\/(localhost(:\d+)?|.*\.yingchu\.cloud)(\/|$)/i;

function _isExternalSenderAllowed(sender) {
  const origin = sender?.origin || (sender?.url ? new URL(sender.url).origin : '');
  if (!origin) return false;
  return ALLOWED_EXTERNAL_ORIGIN_RE.test(origin);
}

chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') {
    sendResponse({ ok: false, error: 'bad_message' });
    return true;
  }
  if (!_isExternalSenderAllowed(sender)) {
    console.warn('[Nexus] external msg from unallowed origin:', sender?.origin);
    sendResponse({ ok: false, error: 'origin_not_allowed' });
    return true;
  }

  const type = msg.type || '';

  if (type === 'nexus.ping') {
    // Web UI uses this to discover whether the extension is installed.
    sendResponse({
      type: 'nexus.pong',
      version: chrome.runtime.getManifest().version,
      protocol_version: PROTOCOL_VERSION,
    });
    return true;
  }

  if (type === 'nexus.setup') {
    const { token, user_id, server_url, device_id } = msg;
    if (!token || !user_id || !server_url) {
      sendResponse({ ok: false, error: 'missing_fields' });
      return true;
    }
    (async () => {
      try {
        await chrome.storage.local.set({
          token,
          user_id,
          server_url,
          device_id: device_id || null,
        });
        // Kick off reconnect now.
        resetBackoff();
        try { _ws?.close(); } catch (_) { /* ignore */ }
        await connect();
        sendResponse({ ok: true, device_id: device_id || null });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }

  if (type === 'nexus.refresh') {
    // Web UI can trigger refresh on demand (e.g. user clicks a
    // "Refresh now" button). The automatic heartbeat path (Wave 2)
    // will call the same helper without going through Web UI.
    (async () => {
      const result = await refreshTokenNow();
      sendResponse(result);
    })();
    return true;
  }

  sendResponse({ ok: false, error: 'unknown_type' });
  return true;
});

/**
 * Fetch a fresh token from the backend using the current token as
 * credential. On success, replace chrome.storage.local.token and
 * nudge the WS so the next reconnect picks up the new auth.
 */
async function refreshTokenNow() {
  const { token, server_url } = await chrome.storage.local.get(['token', 'server_url']);
  if (!token || !server_url) {
    return { ok: false, error: 'no_token_to_refresh' };
  }
  // server_url is wss://host:port — derive http(s)://host:port for the
  // refresh REST endpoint. Bridge exposes /bridge/browser/_ipc/* so
  // refresh lives on the same host root (NOT the Bridge itself — the
  // refresh endpoint is on the API service). We rely on setup having
  // passed a usable wss:// origin; the HTTP schema is a simple swap.
  let apiBase;
  try {
    const u = new URL(server_url.replace(/^wss?:/, (s) =>
      s === 'wss:' ? 'https:' : 'http:'
    ));
    // API typically runs on a different port than Bridge in dev
    // (API 8000 vs Bridge 8001). For now assume the same host; in
    // production both sit behind the same load balancer. Wave 4 will
    // make this explicit via a separate api_url field from the
    // `nexus.setup` payload.
    u.pathname = '';
    u.search = '';
    // Dev: swap port 8001 → 8000. Prod: behind LB on same port.
    if (u.port === '8001') u.port = '8000';
    apiBase = u.origin;
  } catch (e) {
    return { ok: false, error: 'bad_server_url', detail: String(e?.message || e) };
  }

  try {
    const res = await fetch(`${apiBase}/api/v2/browser/token/refresh`, {
      method: 'POST',
      headers: {
        'authorization': `Bearer ${token}`,
        'content-type': 'application/json',
      },
    });
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (_) { /* ignore */ }
      return {
        ok: false,
        error: 'refresh_http_error',
        status: res.status,
        detail: body?.detail || body || 'non-2xx',
      };
    }
    const data = await res.json();
    if (!data?.token) {
      return { ok: false, error: 'refresh_no_token', body: data };
    }
    await chrome.storage.local.set({ token: data.token });
    return { ok: true, ttl: data.ttl, expires_at: data.expires_at };
  } catch (e) {
    return { ok: false, error: 'refresh_network', detail: String(e?.message || e) };
  }
}
