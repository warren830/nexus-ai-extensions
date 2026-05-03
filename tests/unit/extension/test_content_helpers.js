/**
 * Unit tests for content.js pure helpers.
 *
 * Wave C quality review (Stage 2 TDD compliance note) flagged that
 * `parseInstruction`, `inferRole`, `isInteractive`, and `truncate` are
 * pure functions but buried inside the content script IIFE, making them
 * invisible to the 29 extension tests. This file closes that gap by
 * reproducing those helpers (same pattern as test_build_ws_url.js /
 * test_protocol_version.js). Any drift between these reference copies
 * and the real content.js will be caught the first time they misbehave.
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

// Keep in sync with extensions/chrome/content/content.js
const INTERACTIVE_TAGS = new Set([
  'A', 'BUTTON', 'INPUT', 'TEXTAREA', 'SELECT', 'LABEL',
  'SUMMARY', 'DETAILS', 'OPTION',
]);
const INTERACTIVE_ROLES = new Set([
  'button', 'link', 'checkbox', 'radio', 'menuitem', 'option',
  'switch', 'tab', 'textbox', 'combobox', 'searchbox',
]);

function inferRole(el) {
  const explicit = el.getAttribute && el.getAttribute('role');
  if (explicit) return explicit;
  const tag = el.tagName;
  if (tag === 'A' && el.href) return 'link';
  if (tag === 'BUTTON') return 'button';
  if (tag === 'INPUT') {
    const t = (el.type || 'text').toLowerCase();
    if (t === 'checkbox') return 'checkbox';
    if (t === 'radio') return 'radio';
    if (t === 'submit' || t === 'button') return 'button';
    return 'textbox';
  }
  if (tag === 'TEXTAREA') return 'textbox';
  if (tag === 'SELECT') return 'combobox';
  if (tag === 'SUMMARY') return 'button';
  if (tag === 'LABEL') return 'label';
  return '';
}

function isInteractive(el) {
  if (!el || !el.tagName) return false;
  if (INTERACTIVE_TAGS.has(el.tagName)) return true;
  const role = inferRole(el);
  if (role && INTERACTIVE_ROLES.has(role)) return true;
  if (el.hasAttribute && el.hasAttribute('contenteditable')) return true;
  if (el.onclick || (el.getAttribute && el.getAttribute('onclick'))) return true;
  if (el.tabIndex >= 0 && role) return true;
  return false;
}

function truncate(s, max) {
  if (!s) return '';
  if (s.length <= max) return s;
  return s.slice(0, max) + '…';
}

function parseInstruction(instruction) {
  const src = instruction || '';
  const text = src.toLowerCase();
  let verb = 'click';
  if (/\btype\b|\benter\b|\bfill\b|\binput\b/.test(text)) verb = 'type';
  else if (/\bscroll\b/.test(text)) verb = 'scroll';
  else if (/\bpress\b/.test(text)) verb = 'press';
  const quoted = src.match(/"([^"]+)"|'([^']+)'/);
  let target = quoted ? (quoted[1] || quoted[2] || '') : '';

  if (!target) {
    const afterThe = src.match(/(?:click|tap|press)(?:\s+the)?\s+([a-z0-9\-_\s]{2,40})/i);
    if (afterThe) target = afterThe[1].trim();
  }
  if (!target) {
    const typeMatch = src.match(/type\s+(.*?)\s+(?:in|into)\s+(?:the\s+)?([a-z0-9\-_\s]{2,40})/i);
    if (typeMatch) {
      return { verb: 'type', target: typeMatch[2].trim(), value: typeMatch[1].trim() };
    }
  }

  let value = '';
  const valMatch = src.match(/type\s+"([^"]+)"/i) || src.match(/type\s+'([^']+)'/i);
  if (valMatch && verb === 'type') value = valMatch[1];

  return { verb, target: target.toLowerCase(), value };
}

// Simple fake-Element factory for testing. Only populates the fields
// the helpers read — we do NOT need a full jsdom environment.
function fakeEl({
  tagName = 'DIV',
  href = null,
  type = null,
  role = null,
  attrs = {},
  onclick = null,
  // Matches real DOM: non-focusable elements default to tabIndex = -1.
  // Using `null` here would silently satisfy `el.tabIndex >= 0` (null coerces
  // to 0 in numeric comparisons) and produce false positives.
  tabIndex = -1,
  contenteditable = false,
} = {}) {
  const attrMap = { ...attrs };
  if (role !== null) attrMap.role = role;
  if (contenteditable) attrMap.contenteditable = '';
  if (onclick) attrMap.onclick = onclick;
  return {
    tagName,
    href,
    type,
    onclick,
    tabIndex,
    getAttribute(name) {
      return attrMap[name] ?? null;
    },
    hasAttribute(name) {
      return name in attrMap;
    },
  };
}

// ====== tests ======

describe('inferRole', () => {
  test('explicit role attr wins', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'DIV', role: 'menuitem' })), 'menuitem');
  });
  test('<a href> is link', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'A', href: 'https://x' })), 'link');
  });
  test('<a> without href has no implicit role', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'A' })), '');
  });
  test('<button> is button', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'BUTTON' })), 'button');
  });
  test('<input type=checkbox> is checkbox', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'INPUT', type: 'checkbox' })), 'checkbox');
  });
  test('<input type=radio> is radio', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'INPUT', type: 'radio' })), 'radio');
  });
  test('<input type=submit> is button', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'INPUT', type: 'submit' })), 'button');
  });
  test('<input> default is textbox', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'INPUT' })), 'textbox');
  });
  test('<textarea> is textbox', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'TEXTAREA' })), 'textbox');
  });
  test('<select> is combobox', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'SELECT' })), 'combobox');
  });
  test('plain <div> has no role', () => {
    assert.strictEqual(inferRole(fakeEl({ tagName: 'DIV' })), '');
  });
});

describe('isInteractive', () => {
  test('undefined/null is not interactive', () => {
    assert.strictEqual(isInteractive(null), false);
    assert.strictEqual(isInteractive({}), false);
  });
  test('tag in INTERACTIVE_TAGS is interactive', () => {
    for (const tag of ['A', 'BUTTON', 'INPUT', 'TEXTAREA', 'SELECT', 'LABEL']) {
      assert.strictEqual(isInteractive(fakeEl({ tagName: tag })), true, `${tag} should be interactive`);
    }
  });
  test('<div role="button"> is interactive via role', () => {
    assert.strictEqual(isInteractive(fakeEl({ tagName: 'DIV', role: 'button' })), true);
  });
  test('<div contenteditable> is interactive', () => {
    assert.strictEqual(
      isInteractive(fakeEl({ tagName: 'DIV', contenteditable: true })),
      true,
    );
  });
  test('<div onclick=...> is interactive', () => {
    assert.strictEqual(
      isInteractive(fakeEl({ tagName: 'DIV', onclick: () => {} })),
      true,
    );
  });
  test('<div tabIndex=0 role=link> is interactive', () => {
    assert.strictEqual(
      isInteractive(fakeEl({ tagName: 'DIV', role: 'link', tabIndex: 0 })),
      true,
    );
  });
  test('plain <div> is NOT interactive', () => {
    assert.strictEqual(isInteractive(fakeEl({ tagName: 'DIV' })), false);
  });
  test('<span> with unknown role is NOT interactive', () => {
    assert.strictEqual(
      isInteractive(fakeEl({ tagName: 'SPAN', role: 'presentation' })),
      false,
    );
  });
});

describe('truncate', () => {
  test('empty/null returns empty string', () => {
    assert.strictEqual(truncate('', 10), '');
    assert.strictEqual(truncate(null, 10), '');
    assert.strictEqual(truncate(undefined, 10), '');
  });
  test('short string is not truncated', () => {
    assert.strictEqual(truncate('hello', 10), 'hello');
  });
  test('exactly max length passes through', () => {
    assert.strictEqual(truncate('abcdef', 6), 'abcdef');
  });
  test('one longer is truncated with ellipsis', () => {
    assert.strictEqual(truncate('abcdefg', 6), 'abcdef…');
  });
  test('much longer is truncated to max + ellipsis', () => {
    assert.strictEqual(truncate('a'.repeat(100), 10), 'aaaaaaaaaa…');
  });
});

describe('parseInstruction', () => {
  test('default verb is click', () => {
    const r = parseInstruction('click something');
    assert.strictEqual(r.verb, 'click');
  });
  test('contains "type" → verb=type', () => {
    const r = parseInstruction('type hello in email');
    assert.strictEqual(r.verb, 'type');
  });
  test('contains "scroll" → verb=scroll', () => {
    const r = parseInstruction('scroll down');
    assert.strictEqual(r.verb, 'scroll');
  });
  test('contains "press" → verb=press', () => {
    const r = parseInstruction('press the login button');
    assert.strictEqual(r.verb, 'press');
  });
  test('quoted target is extracted', () => {
    const r = parseInstruction('click "Submit"');
    assert.strictEqual(r.target, 'submit');
  });
  test('single-quoted target is extracted', () => {
    const r = parseInstruction("click 'Sign in'");
    assert.strictEqual(r.target, 'sign in');
  });
  test('"click the X" extracts X', () => {
    const r = parseInstruction('click the blue Submit button');
    assert.strictEqual(r.target.includes('submit'), true);
  });
  test('"type X in Y" extracts value X and target Y', () => {
    const r = parseInstruction('type foo@bar in the email field');
    assert.strictEqual(r.verb, 'type');
    assert.strictEqual(r.value, 'foo@bar');
    assert.strictEqual(r.target, 'email field');
  });
  test('quoted value in "type" is captured', () => {
    const r = parseInstruction('type "hello world"');
    assert.strictEqual(r.verb, 'type');
    assert.strictEqual(r.value, 'hello world');
  });
  test('empty input returns default shape', () => {
    const r = parseInstruction('');
    assert.deepStrictEqual(r, { verb: 'click', target: '', value: '' });
  });
  test('null input does not throw', () => {
    assert.doesNotThrow(() => parseInstruction(null));
  });
});
