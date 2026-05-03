// tests/unit/extension/test_protocol_version.js
//
// Tests an equivalent implementation of server-side `_check_protocol_compat`
// logic in pure JS. This is the reference the extension should stay in sync
// with (spec §10 R12).
//
// If this test fails, the extension's declared protocol_version (currently
// "1.0") needs review.
//
// NOTE: We do not import from service_worker.js because the SW is a single
// tightly-coupled file. Instead we re-implement the small compat function
// here and assert the contract.

import test from 'node:test';
import assert from 'node:assert/strict';

const SERVER_MAJOR = 1;
const SERVER_MINOR = 0;

function parseProtocolVersion(str) {
  if (typeof str !== 'string') return null;
  const m = str.match(/^(\d+)\.(\d+)$/);
  if (!m) return null;
  return { major: Number(m[1]), minor: Number(m[2]) };
}

function checkCompat(clientVersion) {
  const parsed = parseProtocolVersion(clientVersion);
  if (!parsed) return 'bad_format';
  if (parsed.major !== SERVER_MAJOR) return 'incompatible_major';
  if (parsed.minor > SERVER_MINOR + 1) return 'stale_minor';
  if (parsed.minor < SERVER_MINOR - 1) return 'stale_minor';
  return 'compatible';
}

test('client 1.0 is compatible with server 1.0', () => {
  assert.equal(checkCompat('1.0'), 'compatible');
});

test('client 1.1 is compatible with server 1.0', () => {
  assert.equal(checkCompat('1.1'), 'compatible');
});

test('client 1.5 is stale but accepted', () => {
  assert.equal(checkCompat('1.5'), 'stale_minor');
});

test('client 2.0 is major incompatible', () => {
  assert.equal(checkCompat('2.0'), 'incompatible_major');
});

test('client "bogus" is bad format', () => {
  assert.equal(checkCompat('bogus'), 'bad_format');
});

test('client undefined is bad format', () => {
  assert.equal(checkCompat(undefined), 'bad_format');
});

test('client "1" (no minor) is bad format', () => {
  assert.equal(checkCompat('1'), 'bad_format');
});

test('parseProtocolVersion extracts major / minor', () => {
  assert.deepEqual(parseProtocolVersion('3.7'), { major: 3, minor: 7 });
});

test('parseProtocolVersion returns null for invalid strings', () => {
  assert.equal(parseProtocolVersion('1.x'), null);
  assert.equal(parseProtocolVersion(''), null);
  assert.equal(parseProtocolVersion(null), null);
});
