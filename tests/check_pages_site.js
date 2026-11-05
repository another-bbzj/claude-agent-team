/* Exercise the static demo boot script inside one vm realm. Node 18+ is optional. */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'docs/site/index.html'), 'utf8');

function extract(id) {
  const marker = '<script id="' + id + '"';
  const start = html.indexOf(marker);
  if (start < 0) throw new Error('missing ' + id);
  const openEnd = html.indexOf('>', start);
  const close = html.indexOf('</script>', openEnd);
  if (openEnd < 0 || close < 0) throw new Error('unclosed ' + id);
  return html.slice(openEnd + 1, close).trim();
}

if (typeof Response !== 'function') {
  console.error('Response is missing; need Node 18+');
  process.exit(1);
}

const scripts = {
  'pages-snapshot': extract('pages-snapshot'),
  'pages-budget-over': extract('pages-budget-over'),
};
const calls = [];
const location = {
  href: 'https://another-bbzj.github.io/claude-agent-team/index.html',
  search: '',
  origin: 'https://another-bbzj.github.io',
  protocol: 'https:',
  host: 'another-bbzj.github.io',
  pathname: '/claude-agent-team/index.html',
  hash: '',
};
const sandbox = {
  document: {
    getElementById: function (id) {
      if (!Object.prototype.hasOwnProperty.call(scripts, id)) return null;
      return { textContent: scripts[id] };
    },
  },
  location: location,
  URL: URL,
  URLSearchParams: URLSearchParams,
  Response: Response,
  Promise: Promise,
  Date: Date,
  JSON: JSON,
  Object: Object,
  Number: Number,
  String: String,
  isFinite: isFinite,
  console: console,
  calls: calls,
};
sandbox.window = sandbox;
sandbox.window.location = location;
sandbox.fetch = function (input) {
  calls.push(String(input));
  return Promise.resolve(new Response('no', { status: 404 }));
};
vm.createContext(sandbox);
vm.runInContext(extract('pages-boot'), sandbox);

const checks = vm.runInContext(`(function () {
  function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }
  var base = JSON.parse(document.getElementById('pages-snapshot').textContent);
  assert(Date.now() === base.generatedAt * 1000, 'clock is not the fixture instant');
  return window.fetch('/snapshot.json?source=all').then(function (res) {
    assert(res.status === 200, 'snapshot status ' + res.status);
    return res.json();
  }).then(function (snap) {
    assert(snap.demo === true, 'demo flag');
    assert(snap.budget.status === 'off', 'budget stays off');
    assert(snap.members.length === 6, 'six synthetic members');
    var stalled = snap.members.filter(function (m) { return m.id === 'synthetic-member-4'; })[0];
    assert(stalled && stalled.name === '演示文档' && stalled.status === 'stalled', 'stalled member');
    var active = (snap.stallAlerts.candidates || []).filter(function (item) {
      return item.idleSeconds >= snap.stallAlerts.thresholdSec;
    }).map(function (item) { return item.id; });
    assert(active.length === 1 && active[0] === 'synthetic-member-4', 'one stall alert');
    assert(calls.length === 0, 'snapshot must not hit the network');
    return window.fetch('/snapshot.json?source=all&session=synthetic-session-wt').then(function (res) { return res.json(); });
  }).then(function (focused) {
    assert(focused.session.id === 'synthetic-session-wt', 'session id');
    assert(focused.session.branch === 'worktree-feature-board', 'branch');
    assert(focused.session.worktree === 'feature-board', 'worktree');
    assert(focused.members.length === 6, 'members stay');
    return window.fetch('/snapshot.json?session=not-a-session').then(function (res) { return res.json(); });
  }).then(function (unknown) {
    assert(unknown.session.id === base.session.id, 'unknown session stays on the fixture');
    location.search = '?budget=over';
    return window.fetch('/snapshot.json?source=all').then(function (res) { return res.json(); });
  }).then(function (over) {
    assert(over.budget.status === 'over', 'page query turns the guard red');
    assert(over.budget.enabled === true, 'budget enabled');
    location.search = '';
    return window.fetch('/snapshot.json?budget=over').then(function (res) { return res.json(); });
  }).then(function (over) {
    assert(over.budget.status === 'over', 'snapshot query turns the guard red');
    return window.fetch('/api/bus/send', { method: 'POST', body: '{}' });
  }).then(function (denied) {
    assert(denied.status === 403, 'post is forbidden');
    return denied.json();
  }).then(function (body) {
    assert(body.error === 'read-only demo: changes are disabled', 'read-only error');
    return window.fetch('/api/agents');
  }).then(function (missing) {
    assert(missing.status === 404, 'api routes stay unavailable');
    return window.fetch('https://evil.invalid/snapshot.json');
  }).then(function (external) {
    assert(external.status === 403, 'external hosts are blocked');
    return window.fetch('backdrops/backdrop.json');
  }).then(function () {
    assert(calls.length === 1 && calls[0] === 'backdrops/backdrop.json', 'relative assets pass through');
    location.protocol = 'file:';
    location.host = '';
    location.href = 'file:///tmp/docs/site/index.html';
    location.origin = 'null';
    return window.fetch('/snapshot.json').then(function (res) {
      assert(res.status === 200, 'file URL still serves the snapshot');
      return JSON.stringify({ ok: true, members: 6, budget: 'over' });
    });
  });
})()`, sandbox);

checks.then(function (text) {
  console.log(text);
}).catch(function (err) {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
