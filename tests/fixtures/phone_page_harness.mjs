// Node harness for tests/test_phone_page_js.py: loads the shipped app.js and the phone page modules against a small
// fake DOM and fetch, then runs one scenario script. Arguments: app.js, static/js/pages dir, scenario JSON.
//
// scenario.routes maps "METHOD /path" (the path after the API prefix, with or without its query: a route without one
// answers any query) to an answer {status, body, delay_ms} or a list of them, consumed in order with the last
// repeating. A request no route names answers 404 with FastAPI's body.
import fs from 'node:fs';
import path from 'node:path';
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
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  // The search's list marks its rows through these; the rows are markup here, so there is nothing to find.
  querySelectorAll() { return []; }
  querySelector() { return null; }
  focus() {}
  select() {}
}

// Every literal id in the Home template: an id="..." attribute, and the ids it passes to a macro (glance_tile's
// detail_id="...", an attrs {"id": "..."}).
const templateIds = new Set(
  [...fs.readFileSync(path.join(pagesDir, '..', '..', '..', 'templates', 'phone.html'), 'utf8')
    .matchAll(/(?:\b|_)id"?(?:=|: )"([^"{}]+)"/g)].map((m) => m[1]),
);

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
  // A route may name the query (GET /work/search?q=fix) or only the path, which then answers any query.
  const queue = queues.get(key) || queues.get(key.split('?')[0]);
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
    // Only the elements phone.html renders exist (its own ids, read from the template), so a module reaching for an
    // id Home does not have finds nothing, as in the browser: the Work header's figure tiles, the CodeRabbit tile,
    // the spend comparison span. app.js looks for the sidebar and its widgets, which this harness leaves out too.
    if (p === 'getElementById') return (id) => (templateIds.has(String(id)) ? elementById(id) : null);
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
  confirms.push({ message, title: options && options.title, details: options && options.details });
  return scenario.confirm !== false;
};
const sockets = [];
globalThis.subscribeSovaSocket = (options) => { sockets.push(options); return { unsubscribe() {} }; };
// Home's own subscription (phone/page.js): the Work header it reuses subscribes as well, for its side figures.
const pageSocket = () => sockets.find((o) => o.onMessage && o.onMessage.name === '_onMessage');

const phone = {
  page: await import(pathToFileURL(pagesDir + '/phone/page.js').href),
  waiting: await import(pathToFileURL(pagesDir + '/phone/waiting.js').href),
  running: await import(pathToFileURL(pagesDir + '/phone/running.js').href),
  start: await import(pathToFileURL(pagesDir + '/phone/start.js').href),
  summary: await import(pathToFileURL(pagesDir + '/phone/summary.js').href),
  finished: await import(pathToFileURL(pagesDir + '/phone/finished.js').href),
  header: await import(pathToFileURL(pagesDir + '/work/header.js').href),
};

const unescape = (s) => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
// The buttons in a section's markup: label (its text, or its aria-label for an icon-only button), disabled, and the
// data-action with its decoded data-args.
function buttons(id) {
  return [...elementById(id).innerHTML.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)].map((m) => {
    const attr = (name) => { const a = m[1].match(new RegExp(' ' + name + '="([^"]*)"')); return a ? unescape(a[1]) : null; };
    const text = unescape(m[2].replace(/<[^>]*>/g, '')).trim();
    return { label: text || attr('aria-label'), disabled: / disabled(?=[\s>])/.test(m[1]), action: attr('data-action'), args: attr('data-args') ? JSON.parse(attr('data-args')) : [] };
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
// Type into Home's search and wait out its debounce, so its results are drawn.
async function search(value) {
  const input = elementById('phone-search-input');
  input.value = value;
  (input.listeners.input || []).forEach((fn) => fn({}));
  await new Promise((r) => realSetTimeout(r, phone.searchDebounce + 20));
  await settle();
}
// Tap the i-th search result, through the list's own click listener.
async function chooseResult(i) {
  const row = { getAttribute: (k) => (k === 'data-search-index' ? String(i) : null) };
  await Promise.all((elementById('phone-search-results').listeners.click || []).map((fn) => fn({ target: { closest: () => row } })));
  await settle();
}

phone.searchDebounce = (await import(pathToFileURL(pagesDir + '/work/search.js').href)).debounceMs();
await settle();
requests.length = 0;
const fn = new Function(
  'phone', 'byId', 'buttons', 'html', 'tap', 'search', 'chooseResult', 'settle', 'requests', 'confirms', 'sockets', 'pageSocket', 'intervals', 'setHidden',
  'return (async () => {' + scenario.script + '})();',
);
const result = await fn(phone, elementById, buttons, html, tap, search, chooseResult, settle, requests, confirms, sockets, pageSocket, intervals, setHidden);
process.stdout.write(JSON.stringify({ result, requests, confirms }));
process.exit(0);
