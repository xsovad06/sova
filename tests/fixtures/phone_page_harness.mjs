// Node harness for tests/test_phone_page_js.py: loads the shipped app.js and the phone page modules against a small
// fake DOM and fetch, then runs one scenario script. Arguments: app.js, static/js/pages dir, scenario JSON.
//
// scenario.routes maps "METHOD /path" (the path after the API prefix) to an answer {status, body, delay_ms} or a list
// of them, consumed in order with the last repeating. A request no route names answers 404 with FastAPI's body.
import fs from 'node:fs';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';

const [appJsPath, pagesDir, scenarioJson] = process.argv.slice(2);
const scenario = JSON.parse(scenarioJson);
const prefix = scenario.prefix || '/api';

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

class FakeEl {
  constructor(id) {
    this.id = id; this.attrs = {}; this.listeners = {}; this.value = ''; this.disabled = false; this.renders = 0; this._html = '';
    this.textContent = ''; this.className = '';
    this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    this.children = []; this.dataset = {};
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); this.children = []; this.renders += 1; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
}

const byId = new Map();
function elementById(id) {
  if (!byId.has(id)) byId.set(id, new FakeEl(id));
  return byId.get(id);
}
const body = new FakeEl('body');
// document.hidden: a scenario starts visible and flips it with setHidden().
let hidden = false;
const setHidden = (v) => { hidden = !!v; };

const requests = [];
const queues = new Map(Object.entries(scenario.routes || {}).map(([k, v]) => [k, Array.isArray(v) ? v.slice() : [v]]));
const realSetTimeout = globalThis.setTimeout;
globalThis.fetch = (input, init) => {
  const url = String(input);
  const method = ((init && init.method) || 'GET').toUpperCase();
  const path = url.startsWith(prefix) ? url.slice(prefix.length) : url;
  const key = method + ' ' + path;
  requests.push({ method, path, body: init && init.body ? JSON.parse(init.body) : undefined, headers: Object.fromEntries(new Headers((init && init.headers) || {})) });
  const queue = queues.get(key);
  const answer = queue ? (queue.length > 1 ? queue.shift() : queue[0]) : { status: 404, body: { detail: 'Not Found' } };
  const respond = () => new Response(answer.body === undefined ? '{}' : JSON.stringify(answer.body), {
    status: answer.status === undefined ? 200 : answer.status, headers: { 'content-type': 'application/json' },
  });
  return answer.delay_ms ? new Promise((r) => realSetTimeout(() => r(respond()), answer.delay_ms)) : Promise.resolve(respond());
};

globalThis.document = new Proxy(absorb(), {
  get(t, p) {
    if (p === 'cookie') return 'sova_csrf=tok';
    if (p === 'hidden') return hidden;
    if (p === 'body') return body;
    // Only the phone page's own elements exist: app.js looks for the sidebar and its widgets, which this page lacks.
    if (p === 'getElementById') return (id) => (String(id).startsWith('phone-') ? elementById(id) : null);
    if (p === 'createElement') return () => new FakeEl('created');
    return t[p];
  },
});
Object.assign(globalThis, {
  window: globalThis, localStorage: absorb(), sessionStorage: absorb(), location: { protocol: 'http:', host: 'test', pathname: '/' },
  EventSource: function () { return absorb(); }, requestAnimationFrame() {}, getComputedStyle: () => ({ getPropertyValue: () => '' }),
});
globalThis.SOVA_API_PREFIX = prefix;
const intervals = [];
globalThis.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
vm.runInThisContext(fs.readFileSync(appJsPath, 'utf8'));
intervals.length = 0;

// After app.js, so the page modules (which call these as globals) get the stubs.
const confirms = [];
globalThis.sovaConfirm = async (message, options) => {
  confirms.push({ message, title: options && options.title });
  return scenario.confirm !== false;
};
const sockets = [];
globalThis.subscribeSovaSocket = (options) => { sockets.push(options); return { unsubscribe() {} }; };

const phone = {
  page: await import(pathToFileURL(pagesDir + '/phone/page.js').href),
  waiting: await import(pathToFileURL(pagesDir + '/phone/waiting.js').href),
  running: await import(pathToFileURL(pagesDir + '/phone/running.js').href),
  start: await import(pathToFileURL(pagesDir + '/phone/start.js').href),
};

const unescape = (s) => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
// The buttons in a section's markup: label, disabled, and the data-action with its decoded data-args.
function buttons(id) {
  return [...elementById(id).innerHTML.matchAll(/<button([^>]*)>([^<]*)<\/button>/g)].map((m) => {
    const attr = (name) => { const a = m[1].match(new RegExp(' ' + name + '="([^"]*)"')); return a ? unescape(a[1]) : null; };
    return { label: unescape(m[2]), disabled: / disabled(?=[\s>])/.test(m[1]), action: attr('data-action'), args: attr('data-args') ? JSON.parse(attr('data-args')) : [] };
  });
}
// An element built with createElement() (renderSectionError's message) is appended, not parsed: show its text.
const html = (id) => elementById(id).innerHTML + elementById(id).children.map((c) => c.textContent).join('');
async function settle() { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); }
// A tap on the button that carries `action` (and `args`, when given), through the page's real click dispatcher.
async function tap(action, args) {
  const el = { getAttribute: (k) => (k === 'data-action' ? action : k === 'data-args' && args ? JSON.stringify(args) : null) };
  await Promise.all((body.listeners.click || []).map((fn) => fn({ target: { closest: () => el }, stopPropagation() {} })));
  await settle();
}
async function submitStart(value) {
  elementById('phone-start-issue').value = value;
  const form = elementById('phone-start-form');
  await Promise.all((form.listeners.submit || []).map((fn) => fn({ preventDefault() {} })));
  await settle();
}

await settle();
requests.length = 0;
const fn = new Function(
  'phone', 'byId', 'buttons', 'html', 'tap', 'submitStart', 'settle', 'requests', 'confirms', 'sockets', 'intervals', 'setHidden',
  'return (async () => {' + scenario.script + '})();',
);
const result = await fn(phone, elementById, buttons, html, tap, submitStart, settle, requests, confirms, sockets, intervals, setHidden);
process.stdout.write(JSON.stringify({ result, requests, confirms }));
process.exit(0);
