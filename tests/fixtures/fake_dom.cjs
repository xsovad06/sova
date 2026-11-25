'use strict';
// A minimal DOM for running the dashboard's dialog code under node: there is no browser or jsdom in this repo.
// It parses the markup the dialogs use (elements, attributes, text, entities), matches the simple selectors they
// query (tag, #id, .class, [attr], [attr="v"], :not(...), comma lists; no combinators), dispatches events through
// a capture and a bubble phase, and models focus and rendering: an element is rendered when it and every
// ancestor are connected and carry neither the `hidden` attribute, the `hidden` class nor style.display "none",
// and only a rendered, focusable element takes focus. Anything it does not model throws rather than guessing.

const VOID = new Set(['area', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'wbr']);
const FOCUSABLE = new Set(['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA']);
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", '#x27': "'", nbsp: ' ', times: '×' };

function decode(text) {
  return text.replace(/&(#?\w+);/g, (m, name) => (name in ENTITIES ? ENTITIES[name] : m));
}

class TextNode {
  constructor(text) { this.nodeType = 3; this.text = text; this.parentNode = null; }
  get textContent() { return this.text; }
}

class Element {
  constructor(doc, tag) {
    this.nodeType = 1;
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.attrs = {};
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = [];
    this.style = { display: '' };
    this.offsetWidth = 0;
  }

  // Attributes and the properties the dialogs read.
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  get id() { return this.getAttribute('id') || ''; }
  set id(v) { this.setAttribute('id', v); }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(v) { if (v) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(v) { if (v) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }
  get type() { return this.getAttribute('type') || ''; }
  set type(v) { this.setAttribute('type', v); }
  get checked() { return this.hasAttribute('checked'); }
  set checked(v) { if (v) this.setAttribute('checked', ''); else this.removeAttribute('checked'); }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().includes(c),
      add: (...cs) => { el.className = Array.from(new Set(list().concat(cs))).join(' '); },
      remove: (...cs) => { el.className = list().filter((c) => !cs.includes(c)).join(' '); },
      toggle: (c, on) => {
        const want = on === undefined ? !list().includes(c) : !!on;
        if (want) el.classList.add(c); else el.classList.remove(c);
        return want;
      },
    };
  }

  // Tree.
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n === this.ownerDocument;
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.childNodes.splice(child.parentNode.childNodes.indexOf(child), 1);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  contains(other) {
    for (let n = other; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }
  // Like innerHTML, the former children are detached; an empty string leaves no text node, as in the DOM.
  set textContent(v) {
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    const text = String(v);
    if (text !== '') this.appendChild(new TextNode(text));
  }
  set innerHTML(html) {
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    parseInto(this, html);
  }
  descendants() {
    const out = [];
    const walk = (el) => el.children.forEach((c) => { out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelectorAll(selector) { return this.descendants().filter((el) => el.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  matches(selector) { return splitList(selector).some((s) => matchCompound(this, s)); }
  closest(selector) {
    for (let n = this; n && n.nodeType === 1 && n !== this.ownerDocument; n = n.parentNode) {
      if (n.matches(selector)) return n;
    }
    return null;
  }

  // Rendering and focus.
  get rendered() {
    if (!this.isConnected) return false;
    for (let n = this; n && n.nodeType === 1; n = n.parentNode) {
      if (n.hidden || n.classList.contains('hidden') || n.style.display === 'none') return false;
    }
    return true;
  }
  getClientRects() { return this.rendered ? [{}] : []; }
  get focusable() {
    if (this.disabled) return false;
    if (FOCUSABLE.has(this.tagName) || (this.tagName === 'A' && this.hasAttribute('href'))) return true;
    return this.hasAttribute('tabindex');
  }
  focus() { if (this.rendered && this.focusable) this.ownerDocument._active = this; }

  // Events.
  addEventListener(type, fn, options) {
    const capture = options === true || !!(options && options.capture);
    const passive = options && typeof options === 'object' ? options.passive : undefined;
    this.listeners.push({ type, fn, capture, passive });
  }
  removeEventListener(type, fn, options) {
    const capture = options === true || !!(options && options.capture);
    this.listeners = this.listeners.filter((l) => !(l.type === type && l.fn === fn && l.capture === capture));
  }
  click() { return dispatch(this, 'click', {}); }
  // An event a module builds itself (a CustomEvent): it takes the same capture and bubble path as every other event,
  // with its detail carried over. Returns false when a listener called preventDefault(), as in the DOM.
  dispatchEvent(event) {
    if (!event || typeof event.type !== 'string') throw new Error('fake_dom: dispatchEvent needs an event with a type');
    return !dispatch(this, event.type, { detail: event.detail }).defaultPrevented;
  }
}

class Document extends Element {
  constructor() {
    super(null, '#document');
    this.ownerDocument = this;
    this._active = null;
    this.body = new Element(this, 'body');
    this.appendChild(this.body);
  }
  get activeElement() {
    return this._active && this._active.isConnected && this._active.rendered ? this._active : this.body;
  }
  get isConnected() { return true; }
  get rendered() { return true; }
  createElement(tag) { return new Element(this, tag); }
  getElementById(id) { return this.querySelector('#' + id); }
}

function splitList(selector) {
  const out = [];
  let depth = 0;
  let cur = '';
  for (const ch of selector) {
    if (ch === '(' || ch === '[') depth++;
    if (ch === ')' || ch === ']') depth--;
    if (ch === ',' && depth === 0) { out.push(cur.trim()); cur = ''; } else cur += ch;
  }
  out.push(cur.trim());
  return out.filter(Boolean);
}

const PART = /^(?:([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]|:not\(((?:[^()]|\([^()]*\))*)\))/;

function matchCompound(el, selector) {
  let rest = selector;
  if (!rest || /\s/.test(rest.replace(/"[^"]*"/g, '""').replace(/\([^)]*\)/g, '()'))) {
    throw new Error('fake_dom: unsupported selector ' + JSON.stringify(selector));
  }
  while (rest) {
    const m = PART.exec(rest);
    if (!m) throw new Error('fake_dom: unsupported selector ' + JSON.stringify(selector));
    if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
    if (m[2] && el.id !== m[2]) return false;
    if (m[3] && !el.classList.contains(m[3])) return false;
    if (m[4] && (!el.hasAttribute(m[4]) || (m[5] !== undefined && el.getAttribute(m[4]) !== m[5]))) return false;
    if (m[6] !== undefined && el.matches(m[6])) return false;
    rest = rest.slice(m[0].length);
  }
  return true;
}

const TOKEN = /<!--[\s\S]*?-->|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=/>]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/g;
const ATTR = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;

function parseInto(root, html) {
  const doc = root.ownerDocument;
  const stack = [root];
  let m;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(html)) !== null) {
    const top = stack[stack.length - 1];
    if (m[1]) {
      const tag = m[1].toUpperCase();
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tagName === tag) { stack.length = i; break; }
      }
    } else if (m[2]) {
      const el = new Element(doc, m[2]);
      let a;
      ATTR.lastIndex = 0;
      while ((a = ATTR.exec(m[3] || '')) !== null) {
        el.attrs[a[1]] = decode(a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '');
      }
      top.appendChild(el);
      if (!m[4] && !VOID.has(m[2].toLowerCase())) stack.push(el);
    } else if (m[5]) {
      if (m[5].trim()) top.appendChild(new TextNode(decode(m[5])));
    }
  }
}

// Runs the listeners along the path: capture from the document down, the target itself, then bubbling back up.
function dispatch(target, type, init) {
  const ev = Object.assign({
    type, target, defaultPrevented: false, _stopped: false, _stoppedNow: false,
    preventDefault() { this.defaultPrevented = true; },
    // stopPropagation() ends the walk after the current node's listeners have all run; only
    // stopImmediatePropagation() also skips the remaining listeners on that same node.
    stopPropagation() { this._stopped = true; },
    stopImmediatePropagation() { this._stopped = true; this._stoppedNow = true; },
  }, init);
  const path = [];
  for (let n = target; n; n = n.parentNode) path.unshift(n);
  const run = (node, capture) => {
    ev.currentTarget = node;
    node.listeners.filter((l) => l.type === type && (node === target || l.capture === capture))
      .forEach((l) => { if (!ev._stoppedNow) l.fn.call(node, ev); });
  };
  for (const node of path.slice(0, -1)) { run(node, true); if (ev._stopped) return ev; }
  run(target, true);
  for (const node of path.slice(0, -1).reverse()) { if (ev._stopped) return ev; run(node, false); }
  return ev;
}

// A key press goes to the focused element, as in a browser.
function key(doc, name, extra) {
  return dispatch(doc.activeElement, 'keydown', Object.assign({ key: name, shiftKey: false }, extra));
}

// A mouse click: the press (pointerdown, then mousedown) and the click land on the same element unless `pressOn`
// says otherwise.
function clickOn(target, pressOn) {
  dispatch(pressOn || target, 'pointerdown', {});
  dispatch(pressOn || target, 'mousedown', {});
  return dispatch(target, 'click', {});
}

module.exports = { Document, key, clickOn, dispatch };
