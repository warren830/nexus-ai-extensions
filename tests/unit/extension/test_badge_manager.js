// tests/unit/extension/test_badge_manager.js
//
// Tests for the pure `computeBadgeConfig()` function in badge_manager.js.
// We intentionally don't exercise `setState()` here — that depends on the
// chrome.action API which has no meaningful test surface outside a real
// browser (covered by Wave C manual QA + Playwright E2E).

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  computeBadgeConfig,
  STATES,
} from '../../../nexus-agent-chrome/background/badge_manager.js';

test('computeBadgeConfig OFFLINE returns offline prefix and empty badge', () => {
  const cfg = computeBadgeConfig(STATES.OFFLINE);
  assert.equal(cfg.iconPrefix, 'icon-offline');
  assert.equal(cfg.badgeText, '');
  assert.match(cfg.title, /offline/i);
});

test('computeBadgeConfig CONNECTING returns connecting prefix with hint', () => {
  const cfg = computeBadgeConfig(STATES.CONNECTING);
  assert.equal(cfg.iconPrefix, 'icon-connecting');
  assert.equal(cfg.badgeText, '');
  assert.match(cfg.title, /connecting/i);
});

test('computeBadgeConfig IDLE shows version in title', () => {
  const cfg = computeBadgeConfig(STATES.IDLE, { version: '0.5.0' });
  assert.equal(cfg.iconPrefix, 'icon-idle');
  assert.equal(cfg.badgeText, '');
  assert.match(cfg.title, /v0\.5\.0/);
});

test('computeBadgeConfig ACTIVE shows session count as badge text', () => {
  const cfg = computeBadgeConfig(STATES.ACTIVE, { sessionCount: 3, version: '0.1.0' });
  assert.equal(cfg.iconPrefix, 'icon-active');
  assert.equal(cfg.badgeText, '3');
  assert.match(cfg.title, /3 sessions/);
});

test('computeBadgeConfig ACTIVE handles singular phrasing', () => {
  const cfg = computeBadgeConfig(STATES.ACTIVE, { sessionCount: 1 });
  assert.equal(cfg.badgeText, '1');
  assert.match(cfg.title, /1 session /);
});

test('computeBadgeConfig ACTIVE clamps session count to 9', () => {
  const cfg = computeBadgeConfig(STATES.ACTIVE, { sessionCount: 42 });
  assert.equal(cfg.badgeText, '9');
});

test('computeBadgeConfig ACTIVE with 0 count still uses active icon', () => {
  const cfg = computeBadgeConfig(STATES.ACTIVE, { sessionCount: 0 });
  assert.equal(cfg.iconPrefix, 'icon-active');
  assert.equal(cfg.badgeText, '');
});

test('computeBadgeConfig ALERT shows ! badge and alertReason in title', () => {
  const cfg = computeBadgeConfig(STATES.ALERT, { alertReason: 'token expired' });
  assert.equal(cfg.iconPrefix, 'icon-alert');
  assert.equal(cfg.badgeText, '!');
  assert.match(cfg.title, /token expired/);
});

test('computeBadgeConfig unknown state falls back to OFFLINE', () => {
  const cfg = computeBadgeConfig('bogus');
  assert.equal(cfg.iconPrefix, 'icon-offline');
});

test('computeBadgeConfig colors are valid hex', () => {
  for (const state of Object.values(STATES)) {
    const cfg = computeBadgeConfig(state, { sessionCount: 2 });
    assert.match(cfg.badgeColor, /^#[0-9a-f]{6}$/i, `bad color for ${state}`);
  }
});
