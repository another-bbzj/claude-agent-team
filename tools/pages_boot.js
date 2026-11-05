/* Inline boot for docs/site/index.html.
   The snapshot JSON elements sit above this script. The clock stays on the
   fixture instant so ages do not drift after the page is published. */
(function () {
  var baseEl = document.getElementById('pages-snapshot');
  var budgetEl = document.getElementById('pages-budget-over');
  if (!baseEl || !budgetEl) throw new Error('static demo snapshot is missing');
  var PAGES_BASE = JSON.parse(baseEl.textContent);
  var PAGES_BUDGET_OVER = JSON.parse(budgetEl.textContent);
  var PAGES_NOW = Number(PAGES_BASE.generatedAt) * 1000;
  if (isFinite(PAGES_NOW)) Date.now = function () { return PAGES_NOW; };

  function clone(value) { return JSON.parse(JSON.stringify(value)); }

  function pagesFocus(snapshot, sessionId) {
    if (!sessionId || !snapshot.session) return snapshot;
    var theaters = snapshot.theaters || [];
    for (var t = 0; t < theaters.length; t++) {
      var theater = theaters[t];
      if (!theater || typeof theater !== 'object') continue;
      var sessions = theater.sessions || [];
      for (var s = 0; s < sessions.length; s++) {
        var item = sessions[s];
        if (!item || typeof item !== 'object' || item.id !== sessionId) continue;
        var session = snapshot.session;
        session.id = item.id;
        if (item.project) session.project = item.project;
        session.branch = item.branch || '';
        session.worktree = item.worktree || '';
        session.theaterId = theater.id || '';
        if (Object.prototype.hasOwnProperty.call(item, 'mtime')) session.lastActivity = item.mtime;
        return snapshot;
      }
    }
    return snapshot;
  }

  function wantsOver(search) {
    var params = new URLSearchParams(search || '');
    if (params.getAll('budget').indexOf('over') !== -1) return true;
    try {
      if (new URLSearchParams(window.location.search).getAll('budget').indexOf('over') !== -1) return true;
    } catch (err) { /* location.search can be unreadable on a file URL */ }
    return false;
  }

  function pagesSnapshot(search) {
    var snap = clone(PAGES_BASE);
    var params = new URLSearchParams(search || '');
    pagesFocus(snap, params.get('session') || '');
    if (wantsOver(search)) snap.budget = clone(PAGES_BUDGET_OVER);
    return snap;
  }

  function pagesJson(status, obj) {
    return new Response(JSON.stringify(obj), {
      status: status,
      headers: { 'Content-Type': 'application/json; charset=utf-8' }
    });
  }

  function requestUrl(input) {
    if (typeof input === 'string') return input;
    if (input && typeof input.url === 'string') return input.url;
    return '';
  }

  function requestMethod(input, init) {
    var method = (init && init.method) || (input && typeof input !== 'string' && input.method) || 'GET';
    return String(method).toUpperCase();
  }

  var pagesRealFetch = (typeof window.fetch === 'function') ? window.fetch.bind(window) : function () {
    return Promise.reject(new Error('offline static demo'));
  };

  function foreignRequest(parsed) {
    if (!parsed) return false;
    var here = window.location;
    if (here.host) return parsed.host !== here.host;
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  }

  window.fetch = function (input, init) {
    var method = requestMethod(input, init);
    var raw = requestUrl(input);
    var parsed = null;
    try { parsed = new URL(raw, window.location.href); } catch (err) { parsed = null; }
    var path = parsed ? parsed.pathname : raw;
    var search = parsed ? parsed.search : '';
    if (method !== 'GET' && method !== 'HEAD') {
      return Promise.resolve(pagesJson(403, { error: 'read-only demo: changes are disabled' }));
    }
    if (foreignRequest(parsed)) {
      return Promise.resolve(pagesJson(403, { error: 'read-only demo: external requests are disabled' }));
    }
    if (path === '/snapshot.json' || path.slice(-14) === '/snapshot.json') {
      return Promise.resolve(pagesJson(200, pagesSnapshot(search)));
    }
    if (path.indexOf('/api/') !== -1) {
      return Promise.resolve(pagesJson(404, { error: 'read-only demo: route unavailable' }));
    }
    return pagesRealFetch(input, init);
  };

  window.__pagesDemo = { snapshot: pagesSnapshot, focus: pagesFocus, now: PAGES_NOW };
})();
