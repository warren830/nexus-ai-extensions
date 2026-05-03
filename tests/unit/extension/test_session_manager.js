// tests/unit/extension/test_session_manager.js
//
// Tests for session_manager.js mapping/auth CRUD, using a minimal in-memory
// chrome.storage.session + chrome.tabs shim.
//
// All assertions target the public async API in session_manager.js.

import test from 'node:test';
import assert from 'node:assert/strict';

/* ---------- install a minimal chrome.* shim BEFORE module load ---------- */

function installChromeShim() {
  const store = new Map();

  const getAll = () => Object.fromEntries(store.entries());

  globalThis.chrome = {
    storage: {
      session: {
        async get(key) {
          if (key == null) return getAll();
          if (typeof key === 'string') {
            return store.has(key) ? { [key]: store.get(key) } : {};
          }
          if (Array.isArray(key)) {
            const out = {};
            for (const k of key) if (store.has(k)) out[k] = store.get(k);
            return out;
          }
          const out = {};
          for (const k of Object.keys(key)) {
            out[k] = store.has(k) ? store.get(k) : key[k];
          }
          return out;
        },
        async set(obj) {
          for (const [k, v] of Object.entries(obj)) store.set(k, v);
        },
        async remove(keys) {
          const arr = Array.isArray(keys) ? keys : [keys];
          for (const k of arr) store.delete(k);
        },
      },
    },
    tabs: {
      _alive: new Set([1001, 1002, 1003]),
      async get(tabId) {
        if (!chrome.tabs._alive.has(tabId)) {
          throw new Error(`No tab with id: ${tabId}`);
        }
        return { id: tabId, status: 'complete' };
      },
    },
  };

  return { store };
}

const { store } = installChromeShim();

// Import AFTER the shim is installed.
const sessionMgr = (await import('../../../nexus-agent-chrome/background/session_manager.js')).default;

function clearStore() { store.clear(); }

test('setTabForSession + getTabForSession round-trips', async () => {
  clearStore();
  await sessionMgr.setTabForSession('chat-abc', 1001);
  assert.equal(await sessionMgr.getTabForSession('chat-abc'), 1001);
});

test('getTabForSession returns null for unknown session', async () => {
  clearStore();
  assert.equal(await sessionMgr.getTabForSession('nope'), null);
});

test('findSessionByTab reverse lookup', async () => {
  clearStore();
  await sessionMgr.setTabForSession('chat-xyz', 1002);
  assert.equal(await sessionMgr.findSessionByTab(1002), 'chat-xyz');
  assert.equal(await sessionMgr.findSessionByTab(9999), null);
});

test('listActiveSessions returns all mappings', async () => {
  clearStore();
  await sessionMgr.setTabForSession('a', 1001);
  await sessionMgr.setTabForSession('b', 1002);
  const active = await sessionMgr.listActiveSessions();
  assert.equal(active.length, 2);
  assert.deepEqual(active.map(x => x.sessionId).sort(), ['a', 'b']);
});

test('activeSessionCount reflects mapping size', async () => {
  clearStore();
  assert.equal(await sessionMgr.activeSessionCount(), 0);
  await sessionMgr.setTabForSession('a', 1001);
  await sessionMgr.setTabForSession('b', 1002);
  assert.equal(await sessionMgr.activeSessionCount(), 2);
});

test('removeSession clears both mapping and auth', async () => {
  clearStore();
  await sessionMgr.setTabForSession('s1', 1001);
  await sessionMgr.grantAuthorization('s1');
  assert.equal(await sessionMgr.isAuthorized('s1'), true);

  await sessionMgr.removeSession('s1');
  assert.equal(await sessionMgr.getTabForSession('s1'), null);
  assert.equal(await sessionMgr.isAuthorized('s1'), false);
});

test('grant and revoke authorization', async () => {
  clearStore();
  assert.equal(await sessionMgr.isAuthorized('s2'), false);

  await sessionMgr.grantAuthorization('s2');
  assert.equal(await sessionMgr.isAuthorized('s2'), true);

  await sessionMgr.revokeAuthorization('s2');
  assert.equal(await sessionMgr.isAuthorized('s2'), false);
});

test('reconcileTabs drops sessions whose tabs are dead', async () => {
  clearStore();
  await sessionMgr.setTabForSession('alive', 1001);
  await sessionMgr.setTabForSession('dead', 4242);     // not in _alive set

  const dead = await sessionMgr.reconcileTabs();
  assert.deepEqual(dead, ['dead']);
  assert.equal(await sessionMgr.getTabForSession('alive'), 1001);
  assert.equal(await sessionMgr.getTabForSession('dead'), null);
});

test('setTabForSession overwrites previous tab for same session', async () => {
  clearStore();
  await sessionMgr.setTabForSession('s', 1001);
  await sessionMgr.setTabForSession('s', 1002);
  assert.equal(await sessionMgr.getTabForSession('s'), 1002);
});

test('idempotent operations — double remove is safe', async () => {
  clearStore();
  await sessionMgr.setTabForSession('s', 1001);
  await sessionMgr.removeSession('s');
  await sessionMgr.removeSession('s');
  assert.equal(await sessionMgr.getTabForSession('s'), null);
});
