/* SOVA Dashboard: driver for a CLI-owned login the server runs for us (claude, gh, google).

   One helper for every page that offers such a login: the Connections page (claude) and the Settings health
   panel (gh, Google). It starts the flow, polls its status, cancels it, and adopts a session that was started
   before the page loaded. It owns no markup: the page renders whatever `onState` reports.

   createReconnectFlow({flow, onState, onCompleted, successMessage}) returns {start, cancel, adopt, stop}.
   onState receives {phase, text, code}: phase is idle | running | completed | failed | timeout | cancelled;
   text is the status line; code is the one-time code a flow such as gh shows, empty until it appears.
   Uses globals from app.js: apiUrl, fetchAPI, csrfHeaders, getErrorDetail, showToast. */

var RECONNECT_FLOW_POLL_MS = 2000;
// Consecutive failed status polls tolerated before the helper stops retrying and leaves the error on screen.
var RECONNECT_FLOW_MAX_POLL_FAILURES = 5;

function createReconnectFlow(opts) {
  var base = '/connections/reconnect/' + opts.flow;
  var timer = null;
  var lastCode = '';
  var pollFailures = 0;
  // Bumped by stop() and cancel(): a request already in flight when that happens compares its captured value
  // and drops its result, so a detached runner never polls, toasts or reloads on its own.
  var generation = 0;

  function emit(phase, text, code) {
    if (phase === 'running' && code) lastCode = code;
    else if (phase !== 'running') lastCode = '';
    opts.onState({phase: phase, text: text || '', code: code || ''});
  }

  function runningText(data) {
    var elapsed = ' (' + Math.round(data.elapsed_seconds) + 's)';
    // A flow that reports a code but has none yet (an older gh, or the line has not printed): never show an
    // empty code. The browser step still happens on the server machine.
    if ('code' in data && !data.code) {
      return 'Waiting for browser login... Check the browser window opened on the server machine.' + elapsed;
    }
    if (data.code) return 'Enter this one-time code on the page that opened in the browser.' + elapsed;
    return 'Waiting for browser login...' + elapsed;
  }

  /* One status payload to one phase: keeps polling while running, otherwise ends in the terminal phase. */
  function applyStatus(data) {
    if (data.status === 'running') {
      emit('running', runningText(data), data.code);
      poll();
      return;
    }
    if (data.status === 'completed') {
      emit('completed', '');
      showToast(opts.successMessage || 'Reconnected', 'success');
      if (opts.onCompleted) opts.onCompleted();
    } else if (data.status === 'idle') {
      emit('idle', '');
    } else {
      emit(data.status, data.detail || data.status);
    }
  }

  function poll() {
    clearTimeout(timer);
    var mine = generation;
    timer = setTimeout(async function () {
      try {
        var data = await fetchAPI(apiUrl(base + '/status'));
        if (mine !== generation) return;
        pollFailures = 0;
        applyStatus(data);
      } catch (e) {
        if (mine !== generation) return;
        pollFailures += 1;
        if (pollFailures < RECONNECT_FLOW_MAX_POLL_FAILURES) {
          // A transient error must not hide a still-valid code mid-flow, and the row recovers on the next good poll.
          emit('running', 'Failed to check reconnect status: ' + e.message, lastCode);
          poll();
        } else {
          // Giving up: leave a non-running phase so the control offers Start again (adopt() or start() resume).
          emit('failed', 'Lost contact with the server: ' + e.message);
        }
      }
    }, RECONNECT_FLOW_POLL_MS);
  }

  /* A session started before this page load is still running server-side: adopt it instead of offering a
     Start button that would only come back with already_in_progress. */
  async function adopt(settleTerminal) {
    var mine = generation;
    pollFailures = 0;
    try {
      var data = await fetchAPI(apiUrl(base + '/status'));
    } catch (e) {
      return;
    }
    if (mine !== generation) return;
    // A page-load adopt only picks up a live session; after a rejected start the session may already be finished.
    if (data.status === 'running' || settleTerminal) applyStatus(data);
  }

  return {
    adopt: adopt,

    start: async function () {
      var mine = generation;
      pollFailures = 0;
      try {
        var res = await fetch(apiUrl(base + '/start'), {method: 'POST', headers: csrfHeaders()});
        if (mine !== generation) return;
        if (!res.ok) {
          emit('failed', await getErrorDetail(res, 'Failed to start reconnect'));
          return;
        }
        var data = await res.json();
        if (mine !== generation) return;
        if (data.status !== 'ok') {
          // Another tab, or an earlier start whose POST was slow, already created the session: attach to it.
          // The session may already have finished, so settle on whatever status it has instead of staying silent.
          if (data.reason === 'already_in_progress') {
            await adopt(true);
            return;
          }
          emit('failed', data.detail || 'Failed to start reconnect');
          return;
        }
        emit('running', 'Waiting for browser login...');
        poll();
      } catch (e) {
        if (mine !== generation) return;
        emit('failed', 'Failed to start reconnect: ' + e.message);
      }
    },

    cancel: async function () {
      clearTimeout(timer);
      generation += 1;
      try {
        var res = await fetch(apiUrl(base + '/cancel'), {method: 'POST', headers: csrfHeaders()});
        if (!res.ok) throw new Error(await getErrorDetail(res, 'HTTP ' + res.status));
      } catch (e) {
        showToast('Failed to cancel: ' + e.message, 'error');
        poll();
        return;
      }
      emit('cancelled', 'Cancelled');
    },

    /* Stops polling without touching the server session, for a page that is about to re-render its control. */
    stop: function () {
      clearTimeout(timer);
      generation += 1;
    },
  };
}
