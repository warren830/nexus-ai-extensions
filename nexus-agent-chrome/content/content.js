// content/content.js
//
// Injected into each Agent Tab by chrome.scripting.executeScript.
// Responds to messages from the service worker:
//   { action: 'observe', query?: string }   → { ok, ax_tree, url, title, truncated }
//   { action: 'act', instruction: string }  → { ok, action_taken, target }
//
// Act is the MVP "naive" matcher: parses the instruction into a verb +
// target phrase, then finds the first ax_tree node whose visible text /
// aria-label / placeholder contains the target phrase, and dispatches the
// corresponding DOM event. No LLM in the browser — the heavy lifting is
// expected to happen server-side in future phases.

(function () {
  // Re-injection guard — Chrome may inject this multiple times.
  if (window.__nexusAgentContentInjected) return;
  window.__nexusAgentContentInjected = true;

  const MAX_AX_NODES = 2000;
  const MAX_TEXT_CHARS = 200;

  const INTERACTIVE_TAGS = new Set([
    'A', 'BUTTON', 'INPUT', 'TEXTAREA', 'SELECT', 'LABEL',
    'SUMMARY', 'DETAILS', 'OPTION',
  ]);
  const INTERACTIVE_ROLES = new Set([
    'button', 'link', 'checkbox', 'radio', 'menuitem', 'option',
    'switch', 'tab', 'textbox', 'combobox', 'searchbox',
  ]);

  /* --- helpers --- */

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (style.opacity === '0') return false;
    return true;
  }

  function inViewport(el) {
    const rect = el.getBoundingClientRect();
    return (
      rect.bottom >= 0 &&
      rect.right >= 0 &&
      rect.top <= (window.innerHeight || document.documentElement.clientHeight) &&
      rect.left <= (window.innerWidth || document.documentElement.clientWidth)
    );
  }

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

  function accessibleName(el) {
    // Best-effort accessible-name per WAI-ARIA guidance: aria-label > aria-
    // labelledby > text content > title > placeholder > value.
    const ariaLabel = el.getAttribute && el.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel.trim();
    const labelledBy = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const ref = document.getElementById(labelledBy);
      if (ref && ref.textContent) return ref.textContent.trim();
    }
    const text = (el.innerText || el.textContent || '').trim();
    if (text) return text;
    const title = el.getAttribute && el.getAttribute('title');
    if (title) return title.trim();
    const placeholder = el.getAttribute && el.getAttribute('placeholder');
    if (placeholder) return placeholder.trim();
    const value = (el.value || '').toString().trim();
    if (value) return value;
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

  /* --- ax_tree builder --- */

  function buildAxTree(query) {
    const all = Array.from(document.querySelectorAll('*'));
    const nodes = [];
    let idSeq = 0;
    let truncated = false;

    const viewportOnly = all.length > MAX_AX_NODES;
    if (viewportOnly) truncated = true;

    for (const el of all) {
      if (!visible(el)) continue;
      if (!isInteractive(el)) continue;
      if (viewportOnly && !inViewport(el)) continue;

      const name = accessibleName(el);
      const role = inferRole(el);
      if (!role && !name) continue;

      if (query) {
        const q = query.toLowerCase();
        const hay = `${role} ${name}`.toLowerCase();
        if (!hay.includes(q)) continue;
      }

      const rect = el.getBoundingClientRect();
      const node = {
        id: `n${idSeq++}`,
        role,
        name: truncate(name, MAX_TEXT_CHARS),
        bbox: {
          x: Math.round(rect.x),
          y: Math.round(rect.y),
          w: Math.round(rect.width),
          h: Math.round(rect.height),
        },
      };

      // Tag & attribute hints to aid server-side matching
      if (el.tagName) node.tag = el.tagName.toLowerCase();
      if (el.type) node.type = el.type;
      if (el.id) node.dom_id = el.id;

      // Stable-ish path for subsequent act lookup
      node.nexus_ref = registerElement(el);

      nodes.push(node);
      if (nodes.length >= MAX_AX_NODES) {
        truncated = true;
        break;
      }
    }

    return {
      ax_tree: nodes,
      url: window.location.href,
      title: document.title || '',
      truncated,
    };
  }

  // Register an element → ephemeral id so the act handler can refer back.
  const _refs = new Map();
  let _refSeq = 0;
  function registerElement(el) {
    const ref = `r${_refSeq++}`;
    _refs.set(ref, el);
    return ref;
  }
  function resolveRef(ref) {
    return _refs.get(ref) || null;
  }

  /* --- Wave 5: ref-based act path ------------------------------
   *
   * Old path: runAct(instruction) parses NL like "click the Submit
   * button", does a full-DOM text match, and clicks the first hit.
   * Falls over on wrapper-heavy SPAs (GitHub PR rows where the <a>
   * is buried 4 layers deep inside <label><div><span>).
   *
   * New path: runActRef({ref, verb, value}) — agent passes the
   * nexus_ref it already received in the observe AX tree. We look
   * up the *exact* element and dispatch directly. No text match.
   *
   * `liftToInteractive(el)` walks up N levels looking for an <a>
   * or [role=link] ancestor — for the case where observe snapshot
   * happened to expose a wrapper (rare, but cheap insurance and
   * it also fixes the adversarial case where observe re-registers
   * the same wrapper element on multiple refs).
   *
   * Old path stays intact as a fallback when ref is missing or
   * stale — see runAct() below.
   * ------------------------------------------------------------ */

  const CLICKABLE_ANCESTOR_TAGS = new Set(['A', 'BUTTON']);
  const CLICKABLE_ANCESTOR_ROLES = new Set([
    'link', 'button', 'menuitem', 'tab', 'option', 'checkbox', 'radio',
  ]);
  const MAX_LIFT_DEPTH = 4;

  function liftToInteractive(el) {
    // Walk up to MAX_LIFT_DEPTH parents looking for a real clickable
    // ancestor. Return the original element if we don't find one.
    if (!el) return null;
    let cur = el;
    for (let i = 0; i <= MAX_LIFT_DEPTH && cur; i++) {
      if (CLICKABLE_ANCESTOR_TAGS.has(cur.tagName)) return cur;
      const role = cur.getAttribute && cur.getAttribute('role');
      if (role && CLICKABLE_ANCESTOR_ROLES.has(role)) return cur;
      if (cur.hasAttribute && cur.hasAttribute('onclick')) return cur;
      cur = cur.parentElement;
    }
    return el;
  }

  function runActRef(params) {
    const { ref, verb, value } = params || {};
    const el = resolveRef(ref);
    if (!el) {
      // Ref stale because page re-rendered between observe and act.
      // Tell the agent specifically so it knows to re-observe rather
      // than wasting another act on the same ref.
      return {
        ok: false,
        error: 'ref_stale',
        action_taken: 'none',
        ref,
        hint: 'Element no longer in DOM. Re-run browser_observe to get fresh refs.',
      };
    }
    if (!document.contains(el)) {
      return { ok: false, error: 'ref_stale', action_taken: 'none', ref };
    }

    // The matching verb is often "click" unless agent explicitly asks
    // type / scroll. Default matches the old text-parse default.
    const v = (verb || 'click').toLowerCase();

    try {
      if (v === 'type') {
        const inputLike =
          el.tagName === 'INPUT' ||
          el.tagName === 'TEXTAREA' ||
          el.isContentEditable;
        // If ref is on a wrapper <div> and there's an input child,
        // type into the child. Lift-down for type, lift-up for click.
        const typeTarget = inputLike
          ? el
          : el.querySelector('input, textarea, [contenteditable]') || el;
        dispatchType(typeTarget, value || '');
        return {
          ok: true,
          action_taken: 'type',
          ref,
          target: accessibleName(typeTarget),
        };
      }
      if (v === 'scroll') {
        el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
        return { ok: true, action_taken: 'scroll', ref };
      }
      // Default = click. Lift to nearest interactive ancestor so a
      // wrapper-ref case still points at a real <a>/<button>.
      const clickTarget = liftToInteractive(el);
      dispatchClick(clickTarget);
      return {
        ok: true,
        action_taken: 'click',
        ref,
        target: accessibleName(clickTarget),
        lifted: clickTarget !== el,
      };
    } catch (e) {
      return {
        ok: false,
        error: 'action_dispatch_failed',
        action_taken: v,
        ref,
        detail: String(e?.message || e),
      };
    }
  }

  /* --- act (naive matcher) --- */

  function parseInstruction(instruction) {
    // Defensive: the first line already coerced instruction to '' for the
    // lowercase branch, but the .match() calls below were called directly
    // on the raw arg and would crash on null/undefined. Keep one canonical
    // string reference.
    const src = instruction || '';
    const text = src.toLowerCase();
    let verb = 'click';
    if (/\btype\b|\benter\b|\bfill\b|\binput\b/.test(text)) verb = 'type';
    else if (/\bscroll\b/.test(text)) verb = 'scroll';
    else if (/\bpress\b/.test(text)) verb = 'press';
    // Best-effort extraction of target phrase inside quotes or after
    // "click the" / "type foo in the bar" etc.
    const quoted = src.match(/"([^"]+)"|'([^']+)'/);
    let target = quoted ? (quoted[1] || quoted[2] || '') : '';

    // Capture phrase after known prepositions
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

  function findCandidate(targetPhrase) {
    if (!targetPhrase) return null;
    const all = Array.from(document.querySelectorAll('*'));
    const q = targetPhrase.toLowerCase();
    for (const el of all) {
      if (!visible(el) || !isInteractive(el)) continue;
      const name = accessibleName(el).toLowerCase();
      const role = inferRole(el);
      if (name.includes(q)) return { el, role, name: accessibleName(el) };
      const attrs = `${el.id || ''} ${el.name || ''} ${el.getAttribute?.('data-testid') || ''}`.toLowerCase();
      if (attrs.includes(q)) return { el, role, name: accessibleName(el) };
    }
    return null;
  }

  function dispatchClick(el) {
    try {
      el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    } catch (e) { /* ignore */ }
    try {
      el.focus?.();
    } catch (e) { /* ignore */ }
    const opts = { bubbles: true, cancelable: true, view: window };
    el.dispatchEvent(new MouseEvent('mousedown', opts));
    el.dispatchEvent(new MouseEvent('mouseup', opts));
    el.click();
  }

  function dispatchType(el, value) {
    try {
      el.focus();
    } catch (e) { /* ignore */ }
    const isNative = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA';
    if (isNative) {
      // Use native setter to defeat React's cached-value short-circuit.
      const proto =
        el.tagName === 'INPUT'
          ? window.HTMLInputElement.prototype
          : window.HTMLTextAreaElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, value);
      else el.value = value;
    } else if (el.isContentEditable) {
      el.textContent = value;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function runScroll(instruction) {
    const lower = (instruction || '').toLowerCase();
    let dy = 300;
    if (/up\b/.test(lower)) dy = -dy;
    window.scrollBy({ top: dy, behavior: 'smooth' });
    return { ok: true, action_taken: 'scroll', target: dy < 0 ? 'up' : 'down' };
  }

  function runAct(instruction) {
    const parsed = parseInstruction(instruction);
    if (parsed.verb === 'scroll') {
      return runScroll(instruction);
    }

    const candidate = findCandidate(parsed.target);
    if (!candidate) {
      return {
        ok: false,
        error: 'target_not_found',
        action_taken: 'none',
        target: parsed.target || '',
      };
    }

    try {
      if (parsed.verb === 'type') {
        dispatchType(candidate.el, parsed.value);
        return {
          ok: true,
          action_taken: 'type',
          target: candidate.name,
        };
      }
      dispatchClick(candidate.el);
      return {
        ok: true,
        action_taken: 'click',
        target: candidate.name,
      };
    } catch (e) {
      return {
        ok: false,
        error: 'action_dispatch_failed',
        action_taken: parsed.verb,
        target: candidate.name,
        detail: String(e?.message || e),
      };
    }
  }

  /* --- message router --- */

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || typeof msg !== 'object') return false;
    try {
      if (msg.action === 'observe') {
        const out = buildAxTree(msg.query || null);
        sendResponse(out);
        return true;
      }
      if (msg.action === 'act') {
        // Wave 5: prefer ref-based path when agent supplies nexus_ref
        // from an earlier observe call. Fall through to legacy
        // instruction parsing when ref is absent or stale.
        if (msg.ref) {
          const out = runActRef({
            ref: msg.ref,
            verb: msg.verb,
            value: msg.value,
          });
          sendResponse(out);
          return true;
        }
        const out = runAct(msg.instruction || '');
        sendResponse(out);
        return true;
      }
    } catch (e) {
      sendResponse({ ok: false, error: 'content_exception', detail: String(e?.message || e) });
      return true;
    }
    return false;
  });
})();
