// The phone page's shared helpers: the one write (a JSON POST that throws an Error carrying the server's message)
// and the one place its sections put markup on the page.
//
// `error.status` is the HTTP status and `error.detail` the response's raw `detail` (an object for a partial plan
// approval, a string for most refusals), so a caller can branch on a 404 or read the per-decision results of a
// 502 without parsing the message.

export async function postJson(path, body, fallback) {
  var opts = { method: 'POST', headers: jsonCsrfHeaders() };
  if (body !== undefined) opts.body = JSON.stringify(body);
  var res = await fetch(apiUrl(path), opts);
  if (!res.ok) {
    var err = new Error(await getErrorDetail(res.clone(), fallback));
    err.status = res.status;
    try {
      var data = await res.json();
      err.detail = data ? data.detail : null;
    } catch (_e) { err.detail = null; }
    throw err;
  }
  return res.json().catch(function() { return {}; });
}

// What each section last drew, so an unchanged poll leaves the DOM alone. A rewrite with the same markup would drop
// the focus on a button, make a screen reader announce every role="alert" again, and send a tap that lands mid-swap
// to a node that is already gone.
var _drawn = {};

// Replace a section's content with markup its builder has already escaped (every interpolation goes through
// escapeHtml(), sovaBtn() or actionStatusHtml()). The sections render here, so that assignment is the page's only
// innerHTML write, and a re-render rebuilds the list contents without touching anything outside the section.
export function setSectionHtml(id, markup) {
  var el = document.getElementById(id);
  if (!el || _drawn[id] === markup) return;
  _drawn[id] = markup;
  el.innerHTML = markup;
}

// A section's load error, drawn once: the same message on the next failed retry leaves the DOM alone as well.
export function setSectionError(id, message) {
  var marker = 'error:' + message;
  if (_drawn[id] === marker) return;
  _drawn[id] = marker;
  renderSectionError(id, message);
}

// An icon from the page's <template id="phone-icons"> (the shared icon macro, rendered by phone.html), as trusted
// markup for sovaBtn()'s iconHtml; '' when the template or the icon is missing.
export function iconHtml(name) {
  var tpl = document.getElementById('phone-icons');
  var el = tpl && tpl.content && tpl.content.querySelector('[data-icon="' + name + '"]');
  return el ? el.innerHTML : '';
}
