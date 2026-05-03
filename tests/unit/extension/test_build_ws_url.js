/**
 * Unit tests for extension buildWsUrl normalization.
 *
 * Runs under `node --test` (Node 18+). The function is copy-pasted from
 * extensions/chrome/background/service_worker.js — this is the same
 * reference-test pattern used by test_protocol_version.js.
 *
 * Added in response to Wave C quality review (P1-1 URL path mismatch +
 * P2 TDD gap). Pins the contract that extension WSS URLs resolve to
 * /bridge/browser/ws on the Bridge service (NOT /api/v2/bridge/...).
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

// Keep this in sync with extensions/chrome/background/service_worker.js
function buildWsUrl(serverUrl, token) {
  let base = serverUrl.replace(/\/+$/, '');
  base = base.replace(/^http:\/\//i, 'ws://').replace(/^https:\/\//i, 'wss://');
  if (!/^wss?:\/\//i.test(base)) {
    base = 'wss://' + base;
  }
  return `${base}/bridge/browser/ws?token=${encodeURIComponent(token)}`;
}

describe('buildWsUrl', () => {
  test('https URL is normalized to wss', () => {
    assert.strictEqual(
      buildWsUrl('https://nexus.example.com', 'tok'),
      'wss://nexus.example.com/bridge/browser/ws?token=tok',
    );
  });

  test('http URL is normalized to ws', () => {
    assert.strictEqual(
      buildWsUrl('http://localhost:8001', 'tok'),
      'ws://localhost:8001/bridge/browser/ws?token=tok',
    );
  });

  test('bare host defaults to wss', () => {
    assert.strictEqual(
      buildWsUrl('nexus.example.com', 'tok'),
      'wss://nexus.example.com/bridge/browser/ws?token=tok',
    );
  });

  test('trailing slashes are stripped', () => {
    assert.strictEqual(
      buildWsUrl('https://nexus.example.com//', 'tok'),
      'wss://nexus.example.com/bridge/browser/ws?token=tok',
    );
  });

  test('already-normalized wss URL is preserved', () => {
    assert.strictEqual(
      buildWsUrl('wss://nexus.example.com:8001', 'tok'),
      'wss://nexus.example.com:8001/bridge/browser/ws?token=tok',
    );
  });

  test('token with URL-unsafe chars is encoded', () => {
    const url = buildWsUrl('https://n.x', 'a/b+c==');
    assert.ok(url.endsWith('?token=a%2Fb%2Bc%3D%3D'), `got ${url}`);
  });

  test('never emits /api/v2 prefix (P1-1 regression guard)', () => {
    // Bridge router mounts at /bridge/*, NOT /api/v2/bridge/*. This is
    // exactly the integration blocker Wave C quality review flagged.
    const samples = [
      'https://nexus.example.com',
      'http://localhost:8001',
      'nexus.example.com',
      'wss://nexus.example.com/extra',
    ];
    for (const input of samples) {
      const url = buildWsUrl(input, 'tok');
      assert.ok(!url.includes('/api/v2'), `URL must not contain /api/v2: ${url}`);
      assert.ok(url.includes('/bridge/browser/ws'), `URL must end at /bridge/browser/ws: ${url}`);
    }
  });

  test('empty token still forms a valid URL (probe path)', () => {
    assert.strictEqual(
      buildWsUrl('https://n.x', ''),
      'wss://n.x/bridge/browser/ws?token=',
    );
  });
});
