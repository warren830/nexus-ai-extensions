// background/badge_manager.js
//
// Toolbar icon state machine (spec §5 Toolbar Icon 状态机).
//
// Five states:
//   OFFLINE     — grey; token not configured / WSS dropped > 30s
//   CONNECTING  — yellow, blinks every 500ms (R19); during handshake / backoff
//   IDLE        — green; WSS up, no active sessions
//   ACTIVE      — purple + N badge; N active sessions
//   ALERT       — red + `!`; token expired / protocol mismatch / offline cmd
//
// This module is a thin wrapper around chrome.action.*; the logic is pure
// so we can unit-test `computeBadgeConfig()` without a real chrome runtime.

export const STATES = Object.freeze({
  OFFLINE: 'OFFLINE',
  CONNECTING: 'CONNECTING',
  IDLE: 'IDLE',
  ACTIVE: 'ACTIVE',
  ALERT: 'ALERT',
});

/**
 * Pure function: given a state + optional context (sessionCount, version,
 * alertReason), return the icon filename prefix, badge text, badge color,
 * and tooltip text.
 *
 * @param {string} state   — one of STATES
 * @param {object} ctx     — { sessionCount, version, alertReason, multiDevice }
 * @returns {object}       — { iconPrefix, badgeText, badgeColor, title }
 */
export function computeBadgeConfig(state, ctx = {}) {
  const version = ctx.version || '0.1.0';
  const sessionCount = Math.max(0, Math.min(9, ctx.sessionCount || 0));
  const alertReason = ctx.alertReason || '';

  switch (state) {
    case STATES.OFFLINE:
      return {
        iconPrefix: 'icon-offline',
        badgeText: '',
        badgeColor: '#888888',
        title: 'Nexus Agent: offline. Open options to configure.',
      };
    case STATES.CONNECTING:
      return {
        iconPrefix: 'icon-connecting',
        badgeText: '',
        badgeColor: '#f5a623',
        title: 'Nexus Agent: connecting…',
      };
    case STATES.IDLE:
      return {
        iconPrefix: 'icon-idle',
        badgeText: '',
        badgeColor: '#22c55e',
        title: `Nexus Agent: connected (v${version})`,
      };
    case STATES.ACTIVE: {
      const badge = sessionCount > 0 ? String(sessionCount) : '';
      return {
        iconPrefix: 'icon-active',
        badgeText: badge,
        badgeColor: '#a855f7',
        title: `Nexus Agent: ${sessionCount} session${sessionCount === 1 ? '' : 's'} running`,
      };
    }
    case STATES.ALERT:
      return {
        iconPrefix: 'icon-alert',
        badgeText: '!',
        badgeColor: '#ef4444',
        title: `Nexus Agent: ${alertReason || 'alert — click to reauthorize'}`,
      };
    default:
      return computeBadgeConfig(STATES.OFFLINE, ctx);
  }
}

let _blinkTimer = null;
let _blinkOn = true;

function _stopBlink() {
  if (_blinkTimer) {
    clearInterval(_blinkTimer);
    _blinkTimer = null;
    _blinkOn = true;
  }
}

// Wave 4: chrome.action.setIcon({path}) silently fails inside MV3
// service workers on some Chromium builds — the internal PNG fetch
// path relies on the DOM's Image element, which SWs don't have.
// Working around by pre-fetching each PNG once, converting to
// ImageData via OffscreenCanvas, and caching the result. Subsequent
// setIcon calls use the cached ImageData and succeed reliably.
//
// Shape: _iconCache[iconPrefix] = { 16: ImageData, 48: ImageData,
//                                    128: ImageData }
const _iconCache = Object.create(null);
let _iconPreloadPromise = null;

async function _loadIconImageData(size, iconPrefix) {
  const url = chrome.runtime.getURL(`icons/${iconPrefix}-${size}.png`);
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`icon fetch ${url} -> ${resp.status}`);
  const blob = await resp.blob();
  const bitmap = await createImageBitmap(blob);
  const canvas = new OffscreenCanvas(size, size);
  const ctx = canvas.getContext('2d');
  ctx.drawImage(bitmap, 0, 0, size, size);
  return ctx.getImageData(0, 0, size, size);
}

async function _preloadIcons(prefixes) {
  // Called lazily on the first setState. Cache survives SW restart
  // because the module reloads, but we only pay the cost once per SW
  // lifetime (a few dozen KB of fetches).
  await Promise.all(
    prefixes.flatMap((prefix) =>
      [16, 48, 128].map(async (size) => {
        try {
          _iconCache[prefix] = _iconCache[prefix] || {};
          _iconCache[prefix][size] = await _loadIconImageData(size, prefix);
        } catch (e) {
          console.warn(
            `[badge] icon preload failed for ${prefix}-${size}:`, e,
          );
        }
      }),
    ),
  );
}

function _applyIcon(iconPrefix, _dim = false) {
  // We no longer implement a dim variant; the blink timer toggles
  // setBadgeText instead (see _startBlink). Simpler + fewer PNGs.
  const cached = _iconCache[iconPrefix];
  if (!cached || !cached['16'] || !cached['48'] || !cached['128']) {
    // First time this prefix is needed — kick off async load and
    // retry after it settles. Swallow errors so a broken PNG doesn't
    // break the state machine.
    if (!_iconPreloadPromise) {
      _iconPreloadPromise = _preloadIcons([
        'icon', 'icon-active', 'icon-alert',
        'icon-connecting', 'icon-idle', 'icon-offline',
      ]).finally(() => {
        _iconPreloadPromise = null;
      });
    }
    _iconPreloadPromise.then(() => {
      const ready = _iconCache[iconPrefix];
      if (ready?.['16'] && ready?.['48'] && ready?.['128']) {
        chrome.action.setIcon({ imageData: ready }).catch(() => {});
      }
    });
    return;
  }
  chrome.action.setIcon({ imageData: cached }).catch((e) => {
    // Still log — if this fires after preload succeeded, something else is wrong.
    console.warn(`[badge] setIcon(${iconPrefix}) failed:`, e);
  });
}

/**
 * Apply the given state to the toolbar icon. Handles blinking for CONNECTING.
 *
 * @param {string} state   — one of STATES
 * @param {object} ctx     — { sessionCount, version, alertReason, multiDevice }
 */
export function setState(state, ctx = {}) {
  _stopBlink();
  const cfg = computeBadgeConfig(state, ctx);

  _applyIcon(cfg.iconPrefix, false);
  try {
    chrome.action.setBadgeText({ text: cfg.badgeText });
    chrome.action.setBadgeBackgroundColor({ color: cfg.badgeColor });
    chrome.action.setTitle({ title: cfg.title });
  } catch (e) {
    // SW may be transient; swallow.
  }

  if (state === STATES.CONNECTING) {
    // Blink the badge text to signal "working" — icon swap alone loses
    // the animation since Wave 4 retired dim-variant PNGs (ImageData
    // cache is per-state, not per-brightness). Text toggle is light
    // weight and gives the user feedback during brief reconnect loops.
    _blinkTimer = setInterval(() => {
      _blinkOn = !_blinkOn;
      try {
        chrome.action.setBadgeText({ text: _blinkOn ? cfg.badgeText : '' });
      } catch {
        /* SW transient */
      }
    }, 500);
  }
}

export default { STATES, computeBadgeConfig, setState };
