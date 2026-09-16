/* SOVA Dashboard -- LLM provider auth status floating widget */

(function () {
  'use strict';

  var POLL_INTERVAL = 60000; // matches the endpoint's cache TTL
  var STORAGE_KEY = 'sova-auth-widget-expanded';

  function init() {
    if (!document.getElementById('auth-widget')) return;

    // Uses globals initWidgetToggle() and visibilityAwarePoll() from app.js
    initWidgetToggle('auth-widget-toggle', 'auth-widget-panel', STORAGE_KEY);
    visibilityAwarePoll(poll, POLL_INTERVAL);
  }

  async function poll() {
    var data;
    try {
      data = await fetchAPI(apiUrl('/auth/status'));
    } catch (_e) {
      renderUnavailable();
      return;
    }

    render(data);
  }

  function render(data) {
    var dot = document.getElementById('auth-widget-dot');
    var label = document.getElementById('auth-widget-label');
    if (dot) {
      dot.className = 'resource-widget-dot ' + dotClass(data);
    }
    if (label) {
      label.textContent = data.provider || 'provider';
    }

    setText('aw-provider-val', data.provider || '--');
    setText('aw-checked-val', formatCheckedAt(data.checked_at));

    var accountSection = document.getElementById('aw-account-section');
    if (data.account) {
      accountSection.classList.remove('hidden');
      var identity = data.account.email || data.account.authMethod || '--';
      setText('aw-account-val', identity);
      var orgParts = [];
      if (data.account.orgName) orgParts.push(data.account.orgName);
      if (data.account.subscriptionType) orgParts.push(data.account.subscriptionType);
      setText('aw-org-val', orgParts.length ? orgParts.join(' / ') : '--');
    } else {
      accountSection.classList.add('hidden');
      setText('aw-account-val', '--');
      setText('aw-org-val', '--');
    }

    var recoverySection = document.getElementById('aw-recovery-section');
    var cmdWrap = document.getElementById('aw-recovery-cmd-wrap');
    var command = data.authenticated ? '' : recoveryCommand(data.provider);
    if (!data.authenticated && (data.detail || command)) {
      recoverySection.classList.remove('hidden');
      setText('aw-recovery-detail', data.detail || '');
      cmdWrap.classList.toggle('hidden', !command);
      setText('aw-recovery-cmd', command);
    } else {
      recoverySection.classList.add('hidden');
      cmdWrap.classList.add('hidden');
      setText('aw-recovery-detail', '');
      setText('aw-recovery-cmd', '');
    }

    var warningsSection = document.getElementById('aw-warnings-section');
    if (data.routing_warnings && data.routing_warnings.length > 0) {
      warningsSection.classList.remove('hidden');
      setText('aw-warnings-cmd', 'unset ' + data.routing_warnings.join(' '));
    } else {
      warningsSection.classList.add('hidden');
      setText('aw-warnings-cmd', '');
    }

    document.getElementById('aw-error-section').classList.add('hidden');
  }

  /* The endpoint's `detail` is a status message ("2.1.259 but not
     authenticated (run: claude auth login)", or a provider-construction error),
     never something a user can paste into a shell. The runnable command is
     derived from the provider instead, so the block under "Recovery command"
     is always literally runnable. Only claude-code has a login command; other
     provider types are configured in sova.toml, not by a CLI login. */
  function recoveryCommand(provider) {
    if (provider !== 'claude-code') return '';
    return 'claude auth login\n# headless or remote (no browser available):\nclaude setup-token';
  }

  function dotClass(data) {
    if (!data.authenticated) return 'resource-dot-red';
    if (data.routing_warnings && data.routing_warnings.length > 0) return 'resource-dot-yellow';
    return 'resource-dot-green';
  }

  function formatCheckedAt(checkedAt) {
    if (!checkedAt) return '--';
    var d = new Date(checkedAt);
    if (isNaN(d.getTime())) return '--';
    return d.toLocaleTimeString();
  }

  function renderUnavailable() {
    // An empty payload already clears every field and hides every optional
    // section; only the label and the error banner differ from a fetched one.
    render({});
    var label = document.getElementById('auth-widget-label');
    if (label) label.textContent = 'unavailable';
    document.getElementById('aw-error-section').classList.remove('hidden');
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
