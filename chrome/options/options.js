// options/options.js
//
// Options page logic:
//   - Navigation between panes
//   - Load/save config (server_url, token, user_id) to chrome.storage.local
//   - Token paste triggers a debounced healthcheck via WSS probe (R19)
//   - Active sessions list from SW (chrome.runtime.sendMessage)
//   - Reconnect button talks to SW

const $ = (sel) => document.querySelector(sel);
const qAll = (sel) => Array.from(document.querySelectorAll(sel));

const els = {
  serverUrl: $('#server-url'),
  token: $('#token'),
  userId: $('#user-id'),
  saveBtn: $('#save-btn'),
  testBtn: $('#test-btn'),
  tokenStatus: $('#token-status'),
  connStatus: $('#connection-status'),
  sessionsList: $('#sessions-list'),
  refreshSessions: $('#refresh-sessions'),
  aboutVersion: $('#about-version'),
  aboutProtocol: $('#about-protocol'),
  openChromeShortcuts: $('#open-chrome-shortcuts'),
};

/* --- Navigation --- */

qAll('.nav-item').forEach((btn) => {
  btn.addEventListener('click', () => {
    qAll('.nav-item').forEach((b) => b.classList.remove('is-active'));
    qAll('.pane').forEach((p) => p.classList.remove('is-active'));
    btn.classList.add('is-active');
    const target = document.getElementById(btn.dataset.target);
    if (target) target.classList.add('is-active');
    if (btn.dataset.target === 'pane-sessions') loadSessions();
  });
});

/* --- Config load --- */

async function loadConfig() {
  const cfg = await chrome.storage.local.get(['server_url', 'token', 'user_id']);
  els.serverUrl.value = cfg.server_url || '';
  els.token.value = cfg.token || '';
  els.userId.value = cfg.user_id || '';
}

/* --- Token normalization ---
 * Valid browser tokens are base64url HMAC payloads: `[A-Za-z0-9_\-/+=]+\.[...]=*`.
 * They NEVER contain whitespace. Terminals / IDE word-wrap / copy-buffer helpers
 * frequently insert spaces or newlines mid-token on paste, which then yields
 * HTTP 403 at the Bridge (HMAC mismatch) and surfaces as WS close 1006 at the
 * Chrome side. Strip every whitespace char (not just edges — `trim()` is not
 * enough) at every read site. */
function readToken() {
  return (els.token.value || '').replace(/\s+/g, '');
}

/* --- URL normalization (must match service worker) --- */

// URL must match service_worker's buildWsUrl. Bridge router is mounted
// at /bridge/* on the Bridge service — no /api/v2 prefix.
function buildWsUrl(serverUrl, token) {
  let base = (serverUrl || '').replace(/\/+$/, '');
  base = base.replace(/^http:\/\//i, 'ws://').replace(/^https:\/\//i, 'wss://');
  if (!/^wss?:\/\//i.test(base)) base = 'wss://' + base;
  return `${base}/bridge/browser/ws?token=${encodeURIComponent(token)}`;
}

/* --- Save --- */

els.saveBtn.addEventListener('click', async () => {
  const serverUrl = els.serverUrl.value.trim();
  const token = readToken();
  const userId = els.userId.value.trim();
  // Echo the cleaned value back so users can see (and rely on) the cleaning.
  if (token !== els.token.value) els.token.value = token;
  if (!serverUrl || !token || !userId) {
    setStatus(els.connStatus, 'Please fill in server URL, token, and user id.', 'err');
    return;
  }
  await chrome.storage.local.set({
    server_url: serverUrl,
    token,
    user_id: userId,
  });
  setStatus(els.connStatus, 'Saved. Asking service worker to reconnect…', 'warn');
  try {
    const resp = await chrome.runtime.sendMessage({ command: 'reconnect' });
    if (resp?.ok) {
      setStatus(els.connStatus, 'Reconnect requested. Watch the toolbar icon.', 'ok');
    } else {
      setStatus(
        els.connStatus,
        `Reconnect failed: ${resp?.error || 'unknown error'}`,
        'err',
      );
    }
  } catch (e) {
    setStatus(els.connStatus, `SW message failed: ${e?.message || e}`, 'err');
  }
});

/* --- Test / probe --- */

els.testBtn.addEventListener('click', async () => {
  const serverUrl = els.serverUrl.value.trim();
  const token = readToken();
  const userId = els.userId.value.trim();
  if (!serverUrl || !token) {
    setStatus(els.tokenStatus, 'Fill in server URL and token first.', 'err');
    return;
  }
  setStatus(els.tokenStatus, 'Probing WSS handshake…', 'warn');
  const result = await probeWss(serverUrl, token, userId);
  if (result.ok) {
    setStatus(els.tokenStatus, `Connected ✓ (${result.detail || 'handshake accepted'})`, 'ok');
  } else {
    setStatus(els.tokenStatus, `✗ ${result.detail || result.error}`, 'err');
  }
});

/* Debounced probe on token paste / change (R19) */

// Minimum token length before we bother probing the server.
// Browser tokens are base64url HMAC payloads ~120+ chars; treat <32 as
// still-typing. This prevents 6s WSS timeouts on every keystroke of a
// hand-typed token (Wave C quality review P2-4).
const MIN_TOKEN_LEN_FOR_PROBE = 32;
const PROBE_DEBOUNCE_MS = 600;

let _probeTimer = null;
function schedulePaste() {
  clearTimeout(_probeTimer);
  _probeTimer = setTimeout(async () => {
    const serverUrl = els.serverUrl.value.trim();
    const token = readToken();
    const userId = els.userId.value.trim();
    if (!serverUrl || !token) return;
    if (token.length < MIN_TOKEN_LEN_FOR_PROBE) {
      // Likely mid-typing. Give a soft hint, don't fire a server probe.
      setStatus(els.tokenStatus, 'Token looks too short — finish pasting…', 'warn');
      return;
    }
    setStatus(els.tokenStatus, 'Checking token…', 'warn');
    const result = await probeWss(serverUrl, token, userId);
    if (result.ok) {
      setStatus(els.tokenStatus, 'Token looks good ✓', 'ok');
    } else {
      setStatus(els.tokenStatus, `✗ ${result.detail || result.error}`, 'err');
    }
  }, PROBE_DEBOUNCE_MS);
}
els.token.addEventListener('paste', () => {
  // Let the paste apply, then scrub whitespace so the user sees what actually
  // ships to the server. Without this, invisible wrapping spaces silently
  // break the handshake (HTTP 403 → client reports WS close 1006).
  setTimeout(() => {
    const cleaned = readToken();
    if (cleaned !== els.token.value) els.token.value = cleaned;
    schedulePaste();
  }, 50);
});
els.token.addEventListener('input', schedulePaste);

/**
 * Probe the server by opening a short-lived WSS connection. We intentionally
 * do _not_ send a real handshake frame — server accepts the socket, then
 * either waits for extension.online (success path), closes with 4001 for
 * invalid token, 4003 for uid mismatch, or 4002 for missing hello within its
 * idle window.
 *
 * We send a best-effort extension.online and interpret close codes.
 */
function probeWss(serverUrl, token, userId) {
  return new Promise((resolve) => {
    let url;
    try {
      url = buildWsUrl(serverUrl, token);
    } catch (e) {
      return resolve({ ok: false, error: 'invalid_url', detail: 'Invalid server URL' });
    }

    let ws;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      return resolve({ ok: false, error: 'create_failed', detail: String(e?.message || e) });
    }

    const timer = setTimeout(() => {
      try { ws.close(); } catch (e) { /* ignore */ }
      resolve({ ok: false, error: 'timeout', detail: 'Probe timed out. Server unreachable?' });
    }, 6000);

    ws.addEventListener('open', () => {
      try {
        ws.send(JSON.stringify({
          type: 'extension.online',
          user_id: userId || '',
          version: '0.1.0-probe',
          protocol_version: '1.0',
          chrome_version: navigator?.userAgent?.match(/Chrome\/(\S+)/)?.[1] || '',
        }));
      } catch (e) { /* ignore */ }
    });

    ws.addEventListener('message', (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'extension.registered') {
          clearTimeout(timer);
          try { ws.close(); } catch (e) { /* ignore */ }
          // TODO(Phase 1.5 / R19): spec §5 Options page prescribes
          //   "Connected as {user_email}"
          // but the server's extension.registered handshake payload
          // (nexus_utils/bridge/router.py:1231) currently only carries
          // connection_id. To match R19 literally, the Bridge router
          // needs to resolve the token's uid → user email (e.g. via the
          // same auth helper Web UI uses) and echo it back in the ack.
          // Until then, showing connection_id keeps the success signal
          // actionable. Tracked in Wave C build-log.md Pass 2 followups.
          resolve({ ok: true, detail: `registered as ${msg.connection_id || 'unknown'}` });
          return;
        }
      } catch (e) { /* ignore */ }
    });

    ws.addEventListener('close', (ev) => {
      clearTimeout(timer);
      if (ev.code === 4001) return resolve({ ok: false, error: 'bad_token', detail: 'Invalid token' });
      if (ev.code === 4002) return resolve({ ok: false, error: 'bad_handshake', detail: 'Bad handshake' });
      if (ev.code === 4003) return resolve({ ok: false, error: 'uid_mismatch', detail: 'User id does not match token' });
      if (ev.code === 4004) return resolve({ ok: false, error: 'protocol', detail: 'Protocol version incompatible' });
      if (ev.wasClean) return resolve({ ok: true, detail: 'Handshake completed, connection closed' });
      return resolve({ ok: false, error: 'closed', detail: `Connection closed (${ev.code})` });
    });

    ws.addEventListener('error', () => {
      // onerror is followed by onclose; handle there.
    });
  });
}

/* --- Active sessions --- */

async function loadSessions() {
  els.sessionsList.innerHTML = '<div class="muted">Loading…</div>';
  try {
    const resp = await chrome.runtime.sendMessage({ command: 'get_status' });
    if (!resp?.ok) {
      els.sessionsList.innerHTML = '<div class="muted">Service worker not responding.</div>';
      return;
    }
    const sessions = resp.sessions || [];
    if (sessions.length === 0) {
      els.sessionsList.innerHTML = '<div class="muted">No active sessions.</div>';
    } else {
      els.sessionsList.innerHTML = '';
      for (const s of sessions) {
        const row = document.createElement('div');
        row.className = 'session-row';
        const info = document.createElement('div');
        info.innerHTML = `
          <div class="tab">Agent Tab #${s.tabId}</div>
          <div class="sid">${escapeHtml(s.sessionId)}</div>
        `;
        const revokeBtn = document.createElement('button');
        revokeBtn.className = 'btn btn-secondary';
        revokeBtn.textContent = 'Revoke';
        revokeBtn.addEventListener('click', async () => {
          revokeBtn.disabled = true;
          revokeBtn.textContent = 'Revoking…';
          await chrome.runtime.sendMessage({
            command: 'revoke_session',
            session_id: s.sessionId,
          });
          loadSessions();
        });
        row.appendChild(info);
        row.appendChild(revokeBtn);
        els.sessionsList.appendChild(row);
      }
    }
    els.aboutVersion.textContent = resp.version || '—';
    els.aboutProtocol.textContent = resp.protocolVersion || '—';
    renderConnStatus(resp);
  } catch (e) {
    els.sessionsList.innerHTML = `<div class="muted">Error: ${escapeHtml(String(e?.message || e))}</div>`;
  }
}

els.refreshSessions.addEventListener('click', loadSessions);

/* --- Connection status (right-side summary on Connection pane) --- */

function renderConnStatus(status) {
  const wsState = status?.wsState || 'unknown';
  const alert = status?.alertReason || '';
  let html = `<strong>WSS state:</strong> ${escapeHtml(wsState)}`;
  if (alert) html += `<br/><strong>Alert:</strong> ${escapeHtml(alert)}`;
  els.connStatus.innerHTML = html;
}

/* --- Open Chrome shortcuts page --- */

els.openChromeShortcuts.addEventListener('click', (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: 'chrome://extensions/shortcuts' });
});

/* --- Utility --- */

function setStatus(el, text, kind /* 'ok' | 'err' | 'warn' */) {
  el.textContent = text;
  el.className = 'status';
  el.classList.add(kind);
  el.classList.remove('hidden');
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]));
}

/* --- Init --- */

(async () => {
  await loadConfig();
  loadSessions();
})();
