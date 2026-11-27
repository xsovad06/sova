// The feed panel's conversation tabs (issue #1544): a fixed Activity tab, one tab per open conversation, and Past
// conversations. A message starts one read-only agent turn on the server (routers/conversations.py); its progress
// arrives as `conversation` messages on the shared status socket (subscribeSovaSocket, never a socket of its own),
// and the stored messages (GET /conversations/{id}) are the source of truth: onOpen refetches every loaded
// conversation, which repairs any delta missed while the socket was down.
//
// State lives in this module behind functions. Each tab keeps its own scroll position and input draft, and a running
// turn keeps running whatever tab is shown.

var _TAB_KEY_PREFIX = 'sova.feed.tab:';
var _ACTIVITY = 'activity';
var _PAST = 'past';

var _convs = {};       // id -> { meta, messages: [] | null }
var _stopping = {};    // id -> true while a Stop was sent and the turn has not ended: its button stays disabled across re-renders
var _openIds = [];     // open conversations, in tab order
var _past = [];        // closed conversations (metadata only), newest first
var _active = _ACTIVITY;
var _scroll = {};      // tab -> scrollTop
var _drafts = {};      // tab -> input text
var _listSeq = 0;      // bumped by every list request and by every local change to the open set
var _sending = false;  // a send request is pending: a second submit would start a duplicate turn
var _convSeq = {};     // id -> sequence of the newest detail request, so an older response never overwrites it

export function activeTab() { return _active; }

function _tabKey() { return _TAB_KEY_PREFIX + (window.SOVA_PROJECT_SLUG || '_'); }

function _el(id) { return document.getElementById(id); }

// Every builder below escapes each value it interpolates (escapeHtml, sovaBtn), so their output is safe markup.
function _setHtml(el, html) { el.innerHTML = html; }

function _json(r) {
  return r.json().catch(function() { return {}; }).then(function(body) {
    if (!r.ok) {
      var err = new Error((body && typeof body.detail === 'string') ? body.detail : 'Request failed (' + r.status + ')');
      err.status = r.status;
      throw err;
    }
    return body;
  });
}

function _post(path, body) {
  return fetch(apiUrl(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined
  }).then(_json);
}

function _tabLabel(meta) {
  var prefix = meta.issue_number ? '#' + meta.issue_number + ' ' : '';
  var title = meta.title || 'Conversation';
  if (meta.issue_number && title === 'Issue #' + meta.issue_number) return '#' + meta.issue_number;
  return prefix + title;
}

function _panelId(tab) {
  if (String(tab) === _ACTIVITY) return 'feed-list';
  if (String(tab) === _PAST) return 'feed-past';
  return 'feed-conversation';
}

// The tab strip. Pure, so a test can call it. Every tab names the panel it shows (aria-controls), and every panel
// names its tab back (aria-labelledby; the shared conversation panel is relabelled on each render).
export function tabsHtml(openMetas, active, pastCount) {
  function tab(id, label, extra) {
    var on = String(active) === String(id);
    var key = escapeHtml(String(id));
    return '<span class="feed-tab' + (on ? ' is-active' : '') + (extra || '') + '">' +
      '<button type="button" role="tab" class="feed-tab-btn" id="feed-tab-' + key + '" aria-controls="' + _panelId(id) +
      '" aria-selected="' + (on ? 'true' : 'false') + '" data-conv-action="show" data-conv-tab="' + key + '">' +
      escapeHtml(label) + '</button>';
  }
  var parts = [tab(_ACTIVITY, 'Activity') + '</span>'];
  openMetas.forEach(function(meta) {
    var key = escapeHtml(String(meta.id));
    parts.push(tab(meta.id, _tabLabel(meta), meta.running ? ' is-running' : '') +
      '<button type="button" class="feed-tab-close" id="feed-tab-close-' + key +
      '" aria-label="Close conversation" data-conv-action="close" data-conv-id="' + key + '">&times;</button></span>');
  });
  parts.push(tab(_PAST, 'Past' + (pastCount ? ' (' + pastCount + ')' : '')) + '</span>');
  return parts.join('');
}

function _messageHtml(m) {
  if (m.role === 'user') {
    return '<div class="feed-msg feed-msg-user"><div class="feed-msg-text">' + escapeHtml(m.content) + '</div></div>';
  }
  var tools = (m.tools || []).map(function(t) { return '<div class="feed-msg-tool">' + escapeHtml(t) + '</div>'; }).join('');
  var body = m.content ? '<div class="feed-msg-text">' + escapeHtml(m.content) + '</div>' : '';
  var foot = '';
  if (m.status === 'running') {
    foot = '<div class="feed-msg-foot"><span class="feed-msg-thinking">Working...</span>' +
      sovaBtn('Stop', { variant: 'secondary', size: 'sm', disabled: !!_stopping[m.conversation_id], attrs: { 'data-conv-action': 'stop', 'data-conv-id': String(m.conversation_id) } }) + '</div>';
  } else if (m.status === 'stopped') {
    foot = '<div class="feed-msg-foot feed-msg-note">Stopped</div>';
  } else if (m.status === 'failed') {
    foot = '<div class="feed-msg-foot feed-msg-error">' + escapeHtml(m.error || 'The turn failed') + '</div>';
  }
  var cost = parseFloat(m.cost_usd) > 0 ? '<span class="feed-line-cost">$' + escapeHtml(parseFloat(m.cost_usd).toFixed(2)) + '</span>' : '';
  return '<div class="feed-msg feed-msg-agent">' + tools + body + foot + (cost ? '<div class="feed-msg-cost">' + cost + '</div>' : '') + '</div>';
}

// One conversation's message list. Pure, so a test can call it.
export function conversationHtml(conv) {
  var meta = conv.meta || {};
  var messages = conv.messages;
  var head = '<div class="feed-conv-head"><span class="feed-conv-title">' + escapeHtml(_tabLabel(meta)) + '</span>' +
    (parseFloat(meta.total_cost_usd) > 0 ? '<span class="feed-line-cost">$' + escapeHtml(parseFloat(meta.total_cost_usd).toFixed(2)) + '</span>' : '') +
    '</div>';
  if (conv.loadError) {
    return head + '<p class="feed-empty feed-msg-error">' + escapeHtml(conv.loadError) + '</p>' +
      sovaBtn('Retry', { variant: 'secondary', size: 'sm', attrs: { 'data-conv-action': 'retry', 'data-conv-id': String(meta.id) } });
  }
  if (messages === null || messages === undefined) return head + '<p class="feed-empty">Loading...</p>';
  if (!messages.length) {
    var hint = meta.issue_number ? 'Ask anything about #' + meta.issue_number + '.' : 'Ask anything about this project.';
    return head + '<p class="feed-empty">' + escapeHtml(hint) + '</p>';
  }
  return head + messages.map(_messageHtml).join('');
}

function _pastHtml() {
  if (!_past.length) return '<p class="feed-empty">No past conversations</p>';
  return _past.map(function(meta) {
    var id = String(meta.id);
    return '<div class="feed-line feed-past-line"><div class="feed-line-main"><div class="feed-line-text">' +
      escapeHtml(_tabLabel(meta)) + '</div><div class="feed-line-actions">' +
      sovaBtn('Reopen', { variant: 'secondary', size: 'sm', attrs: { 'data-conv-action': 'reopen', 'data-conv-id': id } }) +
      sovaBtn('Delete', { variant: 'ghost', size: 'sm', attrs: { 'data-conv-action': 'delete', 'data-conv-id': id } }) +
      '</div></div></div>';
  }).join('');
}

function _scrollerFor(tab) {
  if (tab === _ACTIVITY) return _el('feed-list');
  if (tab === _PAST) return _el('feed-past');
  return _el('feed-conversation');
}

function _renderTabs() {
  var strip = _el('feed-tabs');
  if (!strip) return;
  // Re-rendering replaces the buttons: give focus back to the replacement of the one that held it, and only then,
  // so a background refresh never moves focus.
  var focused = document.activeElement;
  var refocus = focused && focused.id && strip.contains && strip.contains(focused) ? focused.id : null;
  _setHtml(strip, tabsHtml(_openIds.map(function(id) { return _convs[id].meta; }), _active, _past.length));
  var again = refocus ? document.getElementById(refocus) : null;
  if (again && again.focus) again.focus();
}

function _renderBody() {
  var list = _el('feed-list');
  var conv = _el('feed-conversation');
  var past = _el('feed-past');
  if (!list || !conv || !past) return;
  list.hidden = _active !== _ACTIVITY;
  past.hidden = _active !== _PAST;
  conv.hidden = _active === _ACTIVITY || _active === _PAST;
  if (_active === _PAST) _setHtml(past, _pastHtml());
  if (!conv.hidden && _convs[_active]) {
    if (conv.setAttribute) conv.setAttribute('aria-labelledby', 'feed-tab-' + _active);
    var atBottom = conv.scrollHeight - conv.scrollTop - conv.clientHeight < 40;
    _setHtml(conv, conversationHtml(_convs[_active]));
    if (atBottom) conv.scrollTop = conv.scrollHeight;
  }
}

function _render() {
  _renderTabs();
  _renderBody();
}

function _show(tab) {
  var input = _el('feed-input');
  var from = _scrollerFor(_active);
  if (from) _scroll[_active] = from.scrollTop;
  if (input) _drafts[_active] = input.value;
  _active = (tab === _ACTIVITY || tab === _PAST || _convs[tab]) ? tab : _ACTIVITY;
  safeStorageSet(_tabKey(), String(_active));
  _render();
  // On a narrow panel the strip scrolls sideways: keep the tab just shown in view.
  var strip = _el('feed-tabs');
  var activeTab = strip && strip.querySelector ? strip.querySelector('.feed-tab.is-active') : null;
  if (activeTab && activeTab.scrollIntoView) activeTab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  var to = _scrollerFor(_active);
  if (to) to.scrollTop = _scroll[_active] !== undefined ? _scroll[_active] : (to === _el('feed-conversation') ? to.scrollHeight : 0);
  if (input) {
    input.value = _drafts[_active] || '';
    var meta = _convs[_active] && _convs[_active].meta;
    input.placeholder = meta ? 'Ask a follow-up...' : 'Ask SOVA...';
  }
  if (_convs[_active] && _convs[_active].messages === null) _loadConversation(_active);
}

function _loadConversation(id) {
  var seq = _convSeq[id] = (_convSeq[id] || 0) + 1;
  return fetch(apiUrl('/conversations/' + id)).then(_json).then(function(data) {
    if (!_convs[id] || seq !== _convSeq[id]) return;
    var messages = data.messages || [];
    delete data.messages;
    _convs[id] = { meta: data, messages: messages };
    _render();
  }).catch(function(e) {
    if (seq !== _convSeq[id]) return;
    if (e.status === 404) { _forget(id); return; }
    // Only a conversation never loaded is stuck: one already shown keeps its messages until the next catch-up.
    var conv = _convs[id];
    if (conv && conv.messages === null) {
      conv.loadError = e && e.message ? e.message : 'The conversation could not be loaded';
      _render();
    }
  });
}

// The tab the operator is on is gone: show Activity through the normal transition, so its saved draft and scroll
// position come back, then drop what the vanished tab kept.
function _leaveVanishedTab(id) {
  _show(_ACTIVITY);
  delete _drafts[id];
  delete _scroll[id];
}

function _forget(id) {
  _listSeq++;
  delete _convs[id];
  _openIds = _openIds.filter(function(x) { return String(x) !== String(id); });
  if (String(_active) === String(id)) _leaveVanishedTab(id);
  else _render();
}

function _loadList() {
  var seq = ++_listSeq;
  return fetch(apiUrl('/conversations')).then(_json).then(function(data) {
    if (seq !== _listSeq) return;
    var open = data.open || [];
    _openIds = open.map(function(meta) { return meta.id; });
    open.forEach(function(meta) {
      var known = _convs[meta.id];
      _convs[meta.id] = { meta: meta, messages: known ? known.messages : null };
    });
    _past = data.past || [];
    if (_active !== _ACTIVITY && _active !== _PAST && _openIds.indexOf(_active) === -1) _leaveVanishedTab(_active);
    else _render();
  }).catch(function() { /* the strip keeps its last state; the next onOpen retries */ });
}

function _catchUp() {
  _loadList().then(function() {
    _openIds.forEach(function(id) {
      if (_convs[id] && (_convs[id].messages !== null || String(id) === String(_active))) _loadConversation(id);
    });
  });
}

// Messages of one conversation, one copy per id: an incoming copy replaces a stored one only while the stored one is
// still running, so a finished answer fetched first is never put back to running by a slower response.
function _mergeMessages(stored, incoming) {
  var merged = stored.slice();
  incoming.forEach(function(m) {
    var at = merged.findIndex(function(x) { return x.id === m.id; });
    if (at === -1) merged.push(m);
    else if (merged[at].status === 'running' && m.status !== 'running') merged[at] = m;
  });
  return merged;
}

function _hasRunning(messages) {
  return messages.some(function(m) { return m.status === 'running'; });
}

function _addOpen(meta, turn) {
  _listSeq++;
  _convs[meta.id] = { meta: meta, messages: turn ? [turn.user_message, turn.assistant_message] : [] };
  if (turn) _convs[meta.id].meta.running = _hasRunning(_convs[meta.id].messages);
  if (_openIds.indexOf(meta.id) === -1) _openIds.push(meta.id);
  _past = _past.filter(function(p) { return p.id !== meta.id; });
  _drafts[meta.id] = '';
  _show(meta.id);
}

function _fail(e) {
  showToast(e && e.message ? e.message : 'Request failed', 'error');
}

function _send() {
  var input = _el('feed-input');
  if (!input) return;
  var text = input.value.trim();
  if (!text || _sending) return;
  _sending = true;
  input.disabled = true;
  var target = _active;
  var request = _convs[target]
    ? _post('/conversations/' + target + '/messages', { text: text }).then(function(turn) {
        var conv = _convs[target];
        if (conv && conv.messages === null) {
          _loadConversation(target);
        } else if (conv) {
          conv.messages = _mergeMessages(conv.messages, [turn.user_message, turn.assistant_message]);
          conv.meta.running = _hasRunning(conv.messages);
        }
        _drafts[target] = '';
        if (_active === target) input.value = '';
        _render();
      })
    : _post('/conversations', { message: text }).then(function(data) {
        _drafts[target] = '';
        // The operator may have switched tabs meanwhile: the input then holds that tab's draft, kept by _show().
        if (_active === target) input.value = '';
        _addOpen(data.conversation, data.turn);
      });
  // A refused turn stores nothing: the text stays in the input.
  request.catch(_fail).then(function() { _sending = false; input.disabled = false; input.focus(); });
}

function _applyEvent(msg) {
  var id = msg.conversation_id;
  var conv = _convs[id];
  if (!conv) {
    // Started in another browser tab: pick up the new tab and its messages.
    if (msg.event === 'turn_started') _catchUp();
    return;
  }
  if (msg.event === 'turn_started') {
    conv.meta.running = true;
    if (conv.messages !== null && !conv.messages.some(function(m) { return m.id === msg.message_id; })) _loadConversation(id);
    _renderTabs();
    return;
  }
  if (msg.event === 'turn_finished') {
    delete _stopping[id];
    conv.meta.running = false;
    _loadConversation(id);
    _loadList();
    return;
  }
  if (conv.messages === null) return;
  var target = conv.messages.find(function(m) { return m.id === msg.message_id; });
  if (!target) return;
  if (msg.event === 'tool') target.tools = (target.tools || []).concat([msg.text]);
  if (msg.event === 'text') target.content = target.content ? target.content + '\n\n' + msg.text : msg.text;
  if (String(_active) === String(id)) _renderBody();
}

function _onMessage(msg) {
  if (msg && msg.type === 'conversation') _applyEvent(msg);
}

function _onClick(e) {
  var ask = e.target.closest('[data-feed-ask]');
  if (ask) {
    e.preventDefault();
    _post('/conversations', { issue: ask.getAttribute('data-feed-ask') }).then(function(data) {
      _addOpen(data.conversation, null);
      var input = _el('feed-input');
      if (input) input.focus();
    }).catch(_fail);
    return;
  }
  var button = e.target.closest('[data-conv-action]');
  if (!button) return;
  var action = button.getAttribute('data-conv-action');
  var id = button.getAttribute('data-conv-id');
  if (action === 'show') {
    var tab = button.getAttribute('data-conv-tab');
    _show(tab === _ACTIVITY || tab === _PAST ? tab : Number(tab));
  } else if (action === 'retry') {
    if (_convs[Number(id)]) {
      delete _convs[Number(id)].loadError;
      _render();
      _loadConversation(Number(id));
    }
  } else if (action === 'stop') {
    button.disabled = true;
    _stopping[id] = true;
    // A failed stop leaves the turn running: give the button back so it can be tried again.
    _post('/conversations/' + id + '/stop').catch(function(e) {
      delete _stopping[id];
      button.disabled = false;
      _fail(e);
    });
  } else if (action === 'close') {
    _post('/conversations/' + id + '/close').then(function() { _forget(Number(id)); _loadList(); }).catch(_fail);
  } else if (action === 'reopen') {
    _post('/conversations/' + id + '/reopen').then(function(meta) { _addOpen(meta, null); _convs[meta.id].messages = null; _loadConversation(meta.id); }).catch(_fail);
  } else if (action === 'delete') {
    sovaConfirm('Delete this conversation and its messages?', { confirmClass: 'danger', confirmText: 'Delete' }).then(function(ok) {
      if (!ok) return;
      fetch(apiUrl('/conversations/' + id), { method: 'DELETE' }).then(_json).then(function() {
        _past = _past.filter(function(p) { return String(p.id) !== String(id); });
        _forget(Number(id));
      }).catch(_fail);
    });
  }
}

export function init() {
  var panel = _el('feed-panel');
  var form = _el('feed-input-form');
  if (!panel || !form) return;
  var stored = safeStorageGet(_tabKey());
  if (stored === _PAST) _active = _PAST;
  else if (stored && /^\d+$/.test(stored)) _active = Number(stored);
  panel.addEventListener('click', _onClick);
  form.addEventListener('submit', function(e) { e.preventDefault(); _send(); });
  _render();
  subscribeSovaSocket({ onMessage: _onMessage, onOpen: _catchUp, onFallbackPoll: _catchUp, pollIntervalMs: 60000 });
}
