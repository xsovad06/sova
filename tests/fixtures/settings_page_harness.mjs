// Node harness for tests/test_settings_page_js.py: loads the shipped app.js and the Settings page modules
// against a small fake DOM and runs one scenario script. Arguments: app.js, rows.js, state.js, scenario JSON.
import fs from 'node:fs';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';

const [appJsPath, rowsPath, statePath, scenarioJson] = process.argv.slice(2);
const scenario = JSON.parse(scenarioJson);

function absorb() {
  return new Proxy(function () {}, {
    get(_t, p) {
      if (p === 'then') return undefined;
      if (p === Symbol.toPrimitive) return () => '';
      return absorb();
    },
    apply() { return absorb(); },
    construct() { return absorb(); },
    set() { return true; },
  });
}

const all = [];  // every fake element, searched by document.querySelectorAll

// [a="b"][c] compounds, comma-separated: the only selectors the row module asks the document for.
function matches(el, selector) {
  return selector.split(',').some((compound) => {
    const parts = compound.trim().match(/\[[^\]]+\]/g) || [];
    if (!parts.length) return false;
    return parts.every((p) => {
      const m = p.match(/^\[([\w-]+)(?:="((?:[^"\\]|\\.)*)")?\]$/);
      if (!m) return false;
      const value = el.getAttribute(m[1]);
      if (value === null) return false;
      return m[2] === undefined || value === m[2].replace(/\\(.)/g, '$1');
    });
  });
}

function dataAttr(name) { return 'data-' + name.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase()); }

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = {}; this.children = []; this.listeners = {}; this.parts = {};
    this.className = ''; this.textContent = ''; this.type = ''; this.value = '';
    this.disabled = false; this.checked = false; this.hidden = false; this.isConnected = true;
    this.renders = 0; this._html = '';
    const self = this;
    this.dataset = new Proxy({}, {
      get(_t, k) { return typeof k === 'string' ? (self.attrs[dataAttr(k)] ?? undefined) : undefined; },
      set(_t, k, v) { self.attrs[dataAttr(k)] = String(v); return true; },
      deleteProperty(_t, k) { delete self.attrs[dataAttr(k)]; return true; },
    });
    this.classList = {
      add(...c) { const s = new Set(self.className.split(/\s+/).filter(Boolean)); c.forEach((x) => s.add(x)); self.className = [...s].join(' '); },
      remove(...c) { self.className = self.className.split(/\s+/).filter((x) => x && !c.includes(x)).join(' '); },
      contains(c) { return self.className.split(/\s+/).includes(c); },
      toggle(c, on) { if (on === undefined) on = !this.contains(c); on ? this.add(c) : this.remove(c); return on; },
    };
    all.push(this);
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.renders += 1; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return k in this.attrs; }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  closest() { return null; }
  remove() { this.isConnected = false; }
  replaceWith(other) { this.replacedWith = other; this.isConnected = false; }
  descendants() {
    const direct = this.children.concat(Object.values(this.parts));
    return direct.concat(...direct.map((c) => c.descendants()));
  }
  querySelectorAll(sel) { return this.descendants().filter((el) => matches(el, sel)); }
  querySelector(sel) {
    const part = sel.match(/^\[data-part="([\w-]+)"\]$/);
    if (part) {
      if (!this.parts[part[1]]) { const el = new FakeEl('div'); el.setAttribute('data-part', part[1]); this.parts[part[1]] = el; }
      return this.parts[part[1]];
    }
    return this.querySelectorAll(sel)[0] || null;
  }
}

const fetches = [];
let stored = Object.assign({}, scenario.config || {});
globalThis.fetch = (input, init) => {
  const url = String(input);
  const method = ((init && init.method) || 'GET').toUpperCase();
  fetches.push(method + ' ' + url);
  const reply = (r) => Promise.resolve(new Response(r.body, { status: r.status, headers: { 'content-type': 'application/json' } }));
  if (url.endsWith('/settings/config') && method === 'POST') {
    const body = JSON.parse(init.body);
    if (scenario.post.status === 200) stored[body.key] = scenario.readBack !== undefined ? scenario.readBack : body.value;
    return reply(scenario.post);
  }
  if (url.endsWith('/settings/config') && method === 'GET') return reply({ status: 200, body: JSON.stringify({ config: stored }) });
  if (url.endsWith('/settings/config/reset') && method === 'POST') return reply(scenario.reset);
  // GET /models/available: answers are consumed in order, the last one repeating.
  if (url.includes('/models/available')) {
    const queue = scenario.models || [];
    return reply(queue.length > 1 ? queue.shift() : (queue[0] || { status: 500, body: '{}' }));
  }
  return new Promise(() => {});  // anything else (app.js polling, a widget) never answers
};
globalThis.document = new Proxy(absorb(), {
  get(t, p) {
    if (p === 'cookie') return '';
    if (p === 'createElement') return (tag) => new FakeEl(tag);
    if (p === 'createRange') return () => ({ createContextualFragment: () => new FakeEl('fragment') });
    if (p === 'querySelectorAll') return (sel) => all.filter((el) => el.isConnected && matches(el, sel));
    if (p === 'querySelector') return (sel) => all.find((el) => el.isConnected && matches(el, sel)) || null;
    return t[p];
  },
});
Object.assign(globalThis, {
  window: globalThis, localStorage: absorb(), sessionStorage: absorb(), location: absorb(), EventSource: function () { return absorb(); },
  requestAnimationFrame() {}, getComputedStyle: () => ({ getPropertyValue: () => '' }),
});
globalThis.setTimeout = () => 0;
globalThis.setInterval = () => 0;
vm.runInThisContext(fs.readFileSync(appJsPath, 'utf8'));
const toasts = [];
globalThis.showToast = (message, type) => { toasts.push({ message, type }); return { isConnected: true }; };

const state = await import(pathToFileURL(statePath).href);
const rows = await import(pathToFileURL(rowsPath).href);
state.setPayload(scenario.payload);

function el(attrs) { const e = new FakeEl('div'); Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); return e; }
async function settle() { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); }
async function fire(target, type) {
  if (type === 'change' && target.type === 'checkbox') target.checked = !target.checked;
  await Promise.all((target.listeners[type] || []).map((fn) => fn({ target, preventDefault() {}, stopPropagation() {} })));
  await settle();
}
function snap(e) {
  return { renders: e.renders, parts: Object.fromEntries(Object.entries(e.parts).map(([k, p]) => [k, { html: p.innerHTML, text: p.textContent, cls: p.className, hidden: p.hidden, renders: p.renders }])) };
}

// app.js's own startup polls are not the page's doing.
await settle();
fetches.length = 0;
const fn = new Function('rows', 'state', 'el', 'fire', 'settle', 'snap', 'FakeEl', 'return (async () => {' + scenario.script + '})();');
const result = await fn(rows, state, el, fire, settle, snap, FakeEl);
process.stdout.write(JSON.stringify({ result, fetches, toasts }));
