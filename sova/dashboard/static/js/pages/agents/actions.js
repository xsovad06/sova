// Delegated click actions for the Agents page.
//
// Markup that is rebuilt on every poll (task rows, PR rows, agent cards, kanban
// cards) names its handler with data-action and passes arguments as a JSON array
// in data-args, instead of an inline onclick attribute. JSON replaces the old
// hand-escaped JS string literals, so a quote or newline in a title, label or
// merge-blocker text cannot break out of the attribute.
//
// One listener on document.body resolves the closest [data-action] ancestor, which
// is also what lets a nested control (a link or button inside a clickable card)
// win over the card, the way event.stopPropagation() did for the inline handlers.
// It sits on body rather than document so a handler that calls
// event.stopPropagation() still keeps document-level listeners (the card-menu
// closer) from seeing the click.

// No prototype: a data-action named constructor or toString must read as unregistered, not resolve to an
// inherited Object.prototype member and be called with the click's arguments.
var _handlers = Object.create(null);

export function registerActions(map) {
  Object.assign(_handlers, map);
}

export function actionAttrs(name, args) {
  var attrs = ' data-action="' + name + '"';
  if (args && args.length) attrs += ' data-args="' + escapeHtml(JSON.stringify(args)) + '"';
  return attrs;
}

function _dispatch(e) {
  var el = e.target.closest('[data-action]');
  if (!el) return;
  var name = el.getAttribute('data-action');
  var handler = _handlers[name];
  if (!handler) {
    console.warn('No handler registered for data-action', name);
    return;
  }
  var raw = el.getAttribute('data-args');
  var args = [];
  if (raw) {
    try {
      args = JSON.parse(raw);
    } catch (err) {
      // A template mistake would otherwise throw out of the body listener and the click would do nothing, silently.
      console.error('Malformed data-args for data-action', name, raw, err);
      return;
    }
  }
  handler.apply(null, [el, e].concat(args));
}

export function init() {
  registerActions({
    // A control inside a clickable card that must not trigger the card's own action
    // (links, checkboxes): the closest-action lookup already stops at it.
    'stop': function(el, e) { e.stopPropagation(); },
    'navigate': function(el, e, url) { window.location = url; },
  });
  document.body.addEventListener('click', _dispatch);

  // An <img> error event does not bubble, so catch it in the capture phase to hide an <img data-hide-on-error>
  // (renderAssigneeAvatar() in render-utils.js) that failed to load, without an inline onerror attribute.
  // It lives here, with the page-wide click listener, because task rows and PR rows both emit that markup and
  // neither module may depend on the other having been initialised.
  document.addEventListener('error', function(e) {
    if (e.target.matches && e.target.matches('img[data-hide-on-error]')) e.target.style.display = 'none';
  }, true);
}
