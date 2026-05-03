/**
 * Regression tests for token-paste whitespace scrubbing.
 *
 * Bug that justified these tests (2026-05-03 manual E2E):
 *   Terminal word-wrap / copy buffer inserted 3 literal spaces mid-token
 *   (seen as `%20%20%20` in bridge access log). `trim()` does not catch
 *   internal whitespace, so HMAC verification failed, Bridge returned
 *   HTTP 403, and the Chrome side reported WebSocket close 1006. The
 *   options page now strips every whitespace char — this file pins that
 *   behavior so we don't regress back into chasing phantom 1006s.
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

// Keep in sync with extensions/chrome/options/options.js `readToken()`.
function readToken(raw) {
  return (raw || '').replace(/\s+/g, '');
}

describe('options.readToken whitespace scrub', () => {
  test('empty / null / undefined → empty string', () => {
    assert.strictEqual(readToken(''), '');
    assert.strictEqual(readToken(null), '');
    assert.strictEqual(readToken(undefined), '');
  });

  test('clean token passes through untouched', () => {
    const t = 'eyJ1aWQiOiJhYmMifQ.SGVsbG8=';
    assert.strictEqual(readToken(t), t);
  });

  test('leading whitespace stripped', () => {
    assert.strictEqual(readToken('   eyJhIjoxfQ.sig='), 'eyJhIjoxfQ.sig=');
  });

  test('trailing whitespace stripped', () => {
    assert.strictEqual(readToken('eyJhIjoxfQ.sig=\n\n'), 'eyJhIjoxfQ.sig=');
  });

  test('internal spaces stripped (the actual bug)', () => {
    // Reproduces the 2026-05-03 incident: 3 spaces land mid-token from
    // terminal word-wrap on paste.
    const corrupted = 'eyJ1aWQiOiJhYmMiL   CJleHAiOjF9.sig=';
    assert.strictEqual(
        readToken(corrupted),
        'eyJ1aWQiOiJhYmMiLCJleHAiOjF9.sig=',
    );
  });

  test('tabs / newlines / CRLF all stripped', () => {
    const corrupted = 'eyJhIjox\tfQ\n.\r\nsig=';
    assert.strictEqual(readToken(corrupted), 'eyJhIjoxfQ.sig=');
  });

  test('whitespace runs of mixed kinds collapsed', () => {
    const corrupted = ' \t\neyJhIjoxfQ. \n sig= \t';
    assert.strictEqual(readToken(corrupted), 'eyJhIjoxfQ.sig=');
  });

  test('token with only whitespace → empty (caller can then reject)', () => {
    assert.strictEqual(readToken('   \n\t  '), '');
  });

  test('non-whitespace unicode survives (no overreach into payload bytes)', () => {
    // Unlikely in base64url but guards against regex overreach.
    const t = 'eyJ1aWQiOiLlpKflpLoifQ.sig=';
    assert.strictEqual(readToken(t), t);
  });
});
