/**
 * Nexus Agent toolbar popup.
 *
 * What the user sees depends on chrome.storage.local state:
 *
 *   Has token + ws online  → green/blue dot, device meta, Disconnect visible
 *   Has token + ws offline → gray/red dot, device meta, Disconnect visible
 *   No token               → onboarding text, no Disconnect
 *
 * The popup reads state from chrome.storage and, for the live WS status,
 * asks the service worker via runtime.sendMessage({type: 'popup.status'}).
 * The SW is authoritative for WS state since storage doesn't track it.
 *
 * Disconnect is a LOCAL operation: it clears chrome.storage token +
 * device_id + server_url and tells the SW to close the WS. It does NOT
 * hit the server — the server-side device row stays (the user can
 * reconnect later via the Web UI without a fresh Revoke/Reinstate
 * cycle). If the user wants the server row gone too, they revoke from
 * Settings → Browser Extension.
 */

const $ = (id) => document.getElementById(id);

function renderNoToken(manifestVersion) {
  $('version').textContent = `v${manifestVersion}`;
  $('status-dot').className = 'dot dot-offline';
  $('status-label').textContent = 'Not connected';
  $('meta-block').hidden = true;
  $('onboard-block').hidden = false;
  $('btn-disconnect').hidden = true;
}

function renderWithToken(storage, swStatus, manifestVersion) {
  $('version').textContent = `v${manifestVersion}`;
  $('meta-block').hidden = false;
  $('onboard-block').hidden = true;
  $('btn-disconnect').hidden = false;

  // Status pill follows the SW-reported state machine. Fallbacks for
  // when SW is suspended and doesn't respond — at least show that the
  // extension has credentials.
  const state = (swStatus && swStatus.state) || 'unknown';
  const label = (swStatus && swStatus.label) || 'Status unknown (wake SW?)';
  $('status-dot').className = `dot dot-${state.toLowerCase()}`;
  $('status-label').textContent = label;

  $('device-id').textContent =
    storage.device_id ? `${storage.device_id.slice(0, 20)}…` : '—';
  // Full device_id stashed on dataset for the copy handler.
  $('device-id').dataset.full = storage.device_id || '';

  $('server-url').textContent = storage.server_url || '—';
  $('server-url').title = storage.server_url || '';
}

async function getStorage() {
  return new Promise((r) =>
    chrome.storage.local.get(
      ['token', 'user_id', 'server_url', 'device_id'],
      r,
    ),
  );
}

async function queryServiceWorkerStatus() {
  // sendMessage with chrome.runtime.id targets the SAME extension SW.
  // 300ms timeout: SW should respond within one event-loop tick; if
  // it's suspended, Chrome wakes it + the round-trip is still fast.
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), 800);
    try {
      chrome.runtime.sendMessage({ type: 'popup.status' }, (resp) => {
        clearTimeout(timer);
        if (chrome.runtime.lastError) {
          resolve(null);
        } else {
          resolve(resp);
        }
      });
    } catch {
      clearTimeout(timer);
      resolve(null);
    }
  });
}

async function render() {
  const manifest = chrome.runtime.getManifest();
  const [storage, swStatus] = await Promise.all([
    getStorage(),
    queryServiceWorkerStatus(),
  ]);
  if (!storage.token) {
    renderNoToken(manifest.version);
  } else {
    renderWithToken(storage, swStatus, manifest.version);
  }
}

$('device-id').addEventListener('click', async () => {
  const full = $('device-id').dataset.full;
  if (!full) return;
  try {
    await navigator.clipboard.writeText(full);
    const orig = $('device-id').textContent;
    $('device-id').textContent = 'Copied!';
    setTimeout(() => {
      $('device-id').textContent = orig;
    }, 1200);
  } catch {
    /* ignore — best-effort */
  }
});

$('btn-settings').addEventListener('click', async () => {
  // NEXUS_WEB_UI_URL is baked into the extension at build time (Wave 4);
  // when unset we fall back to the stored server_url by deriving its
  // origin. Last resort: localhost:3000 (dev default).
  const stored = await getStorage();
  let base = '';
  try {
    if (typeof NEXUS_WEB_UI_URL !== 'undefined' && NEXUS_WEB_UI_URL) {
      base = String(NEXUS_WEB_UI_URL).replace(/\/+$/, '');
    }
  } catch { /* unset */ }
  if (!base && stored.server_url) {
    try {
      const u = new URL(
        stored.server_url.replace(/^wss?:/, (s) =>
          s === 'wss:' ? 'https:' : 'http:',
        ),
      );
      // server_url points at Bridge (:8001); Web UI is usually :3000.
      // We can't assume that mapping at runtime, so just strip the port
      // and hope for the best. This is why Wave 4's build-time injection
      // is the right fix.
      base = `${u.protocol}//${u.hostname}:3000`;
    } catch { /* malformed */ }
  }
  if (!base) base = 'http://localhost:3000';
  chrome.tabs.create({ url: `${base}/settings/browser-extension` });
  window.close();
});

$('btn-disconnect').addEventListener('click', async () => {
  // Local disconnect: wipe credentials + ask SW to close WS. Server
  // row untouched — this matches the "local logout" semantics, not
  // "revoke." See module comment.
  await chrome.storage.local.remove([
    'token', 'user_id', 'server_url', 'device_id',
  ]);
  try {
    chrome.runtime.sendMessage({ type: 'popup.disconnect' }, () => {
      if (chrome.runtime.lastError) {
        /* SW asleep — storage wipe is the real effect anyway */
      }
    });
  } catch {
    /* ignore */
  }
  // Re-render with no-token state instead of closing — gives the user
  // visual confirmation the disconnect landed.
  render();
});

render();
