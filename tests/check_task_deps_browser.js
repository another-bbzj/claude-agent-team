'use strict';
/* Headless check for official-task dependency arcs. Invoked by tests/test_task_deps_ui.py. */
const http = require('http');
const fs = require('fs');
const { spawn } = require('child_process');

const pageUrl = process.argv[2];
const chromePath = process.argv[3];
const debugPort = process.argv[4];
const userData = process.argv[5];
const shotDir = process.argv[6] || '';

function getJson(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
        catch (err) { reject(err); }
      });
    });
    req.on('error', reject);
  });
}

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function waitJson(url) {
  let last = 'not started';
  for (let i = 0; i < 50; i += 1) {
    try { return await getJson(url); }
    catch (err) { last = String(err); await sleep(200); }
  }
  throw new Error('chrome debug port not ready: ' + last);
}

const chrome = spawn(chromePath, [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  '--disable-dev-shm-usage',
  '--disable-extensions',
  '--no-first-run',
  '--no-default-browser-check',
  '--window-size=1280,900',
  '--remote-debugging-port=' + debugPort,
  '--user-data-dir=' + userData,
  'about:blank',
], { stdio: 'ignore' });

let socket = null;
function shutdown() {
  try { if (socket) socket.close(); } catch (err) { /* already closed */ }
  try { chrome.kill('SIGKILL'); } catch (err) { /* already gone */ }
}
process.on('exit', shutdown);

function connect(url) {
  const ws = new WebSocket(url);
  socket = ws;
  const pending = new Map();
  let seq = 0;
  const ready = new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve);
    ws.addEventListener('error', () => reject(new Error('devtools websocket failed')));
  });
  ws.addEventListener('message', (event) => {
    const msg = JSON.parse(event.data);
    if (!msg.id || !pending.has(msg.id)) return;
    const done = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) done.reject(new Error(JSON.stringify(msg.error)));
    else done.resolve(msg.result);
  });
  function send(method, params, sessionId) {
    return new Promise((resolve, reject) => {
      const id = seq + 1;
      seq = id;
      pending.set(id, { resolve, reject });
      const payload = { id: id, method: method, params: params || {} };
      if (sessionId) payload.sessionId = sessionId;
      ws.send(JSON.stringify(payload));
    });
  }
  return ready.then(() => send);
}

const PAGE = `(function () {
  function card(id) {
    const cards = document.querySelectorAll('#kanban .kcard');
    for (let i = 0; i < cards.length; i += 1) if (cards[i].dataset.task === id) return cards[i];
    return null;
  }
  function fire(el, type, extra) {
    const rect = el.getBoundingClientRect();
    el.dispatchEvent(new PointerEvent(type, Object.assign({
      bubbles: true, cancelable: true, composed: true, pointerId: 1, pointerType: 'mouse',
      clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2,
    }, extra || {})));
  }
  function hover(id) {
    const el = card(id);
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    fire(el, 'pointerover');
    return el;
  }
  function leave(el) {
    fire(el, 'pointerout', { relatedTarget: document.body });
  }
  function arcs() {
    return Array.from(document.querySelectorAll('#kdeps .kdep-link')).map((el) => ({
      target: el.getAttribute('data-dep-target'),
      open: el.getAttribute('data-dep-open'),
      side: el.getAttribute('data-dep-side'),
      label: el.getAttribute('aria-label') || '',
    }));
  }
  function pinned() {
    return (document.querySelector('#task-board-link').textContent || '').indexOf('已钉住') >= 0;
  }
  function detail() {
    const root = document.querySelector('#task-detail');
    return { open: !root.hidden, title: document.querySelector('#td-title').textContent };
  }
  function strokeOf(selector) {
    const el = document.querySelector(selector);
    if (!el) return '';
    const style = getComputedStyle(el);
    return style.stroke + ' ' + style.strokeWidth + ' ' + style.strokeDasharray;
  }
  function hit(id) {
    const el = card(id);
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    const rect = el.getBoundingClientRect();
    const center = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    const end = document.querySelector('#kdeps .kdep-end');
    const endRect = end ? end.getBoundingClientRect() : null;
    const endHit = endRect ? document.elementFromPoint(endRect.left + endRect.width / 2, endRect.top + endRect.height / 2) : null;
    const path = document.querySelector('#kdeps .kdep');
    const box = path ? path.getBBox() : null;
    const svg = document.querySelector('#kdeps').getBoundingClientRect();
    return {
      centerInCard: !!(center && center.closest && center.closest('.kcard') === el),
      endInArc: !!(endHit && endHit.closest && endHit.closest('#kdeps')),
      box: box ? { x: box.x, y: box.y, w: box.width, h: box.height } : null,
      svgWidth: svg.width,
    };
  }
  document.documentElement.setAttribute('data-theme', 'light');
  document.getElementById('task-board').scrollIntoView({ block: 'start' });
  const idle = arcs().length;
  card('1').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, altKey: true }));
  const alt = { pinned: pinned(), detail: detail() };
  const openEdge = arcsAfter('3');
  const openHit = hit('3');
  document.querySelector('#kdeps .kdep-end').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  const afterArc = { pinned: pinned(), detail: detail(), for: document.querySelector('#kdeps').getAttribute('data-dep-for'), arcs: arcs() };
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  const afterEsc = { pinned: pinned(), detail: detail() };
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  const afterUnpin = { pinned: pinned(), detail: detail() };
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  leave(card('1'));
  const cleared = arcs().length;
  hover('4');
  const doneEdge = { arcs: arcs(), for: document.querySelector('#kdeps').getAttribute('data-dep-for') };
  const link = document.querySelector('#kdeps .kdep-link');
  link.focus();
  link.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  const afterKey = { pinned: pinned(), detail: detail() };
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  hover('2');
  const doneFromBlocker = arcs();
  const doneStroke = strokeOf('#kdeps .kdep.done');
  hover('1');
  const openFromBlocker = arcs();
  const light = strokeOf('#kdeps .kdep.open');
  document.documentElement.setAttribute('data-theme', 'dark');
  const dark = strokeOf('#kdeps .kdep.open');
  function arcsAfter(id) {
    hover(id);
    return { arcs: arcs(), for: document.querySelector('#kdeps').getAttribute('data-dep-for') };
  }
  return { idle: idle, alt: alt, openEdge: openEdge, openHit: openHit, afterArc: afterArc, afterEsc: afterEsc, afterUnpin: afterUnpin, cleared: cleared, doneEdge: doneEdge, afterKey: afterKey, doneFromBlocker: doneFromBlocker, openFromBlocker: openFromBlocker, light: light, dark: dark, doneStroke: doneStroke };
})()`;

function prepare(theme, id) {
  return `(function () {
    document.documentElement.setAttribute('data-theme', ${JSON.stringify(theme)});
    const cards = document.querySelectorAll('#kanban .kcard');
    let el = null;
    for (let i = 0; i < cards.length; i += 1) if (cards[i].dataset.task === ${JSON.stringify(id)}) el = cards[i];
    document.getElementById('task-board').scrollIntoView({ block: 'start' });
    const rect = el.getBoundingClientRect();
    el.dispatchEvent(new PointerEvent('pointerover', {
      bubbles: true, cancelable: true, composed: true, pointerId: 1, pointerType: 'mouse',
      clientX: rect.left + 20, clientY: rect.top + 20,
    }));
    return true;
  })()`;
}

(async () => {
  const version = await waitJson('http://127.0.0.1:' + debugPort + '/json/version');
  const send = await connect(version.webSocketDebuggerUrl);
  const created = await send('Target.createTarget', { url: 'about:blank' });
  const attached = await send('Target.attachToTarget', { targetId: created.targetId, flatten: true });
  const sessionId = attached.sessionId;
  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);
  async function evaluate(expression) {
    const result = await send('Runtime.evaluate', {
      expression: expression,
      awaitPromise: true,
      returnByValue: true,
    }, sessionId);
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result && result.result.value;
  }
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1280, height: 900, deviceScaleFactor: 1, mobile: false,
  }, sessionId);
  await send('Page.navigate', { url: pageUrl }, sessionId);
  await evaluate(`new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      if (document.querySelectorAll('#kanban .kcard').length >= 5) resolve(true);
      else if (Date.now() - start > 8000) reject(new Error('board not ready'));
      else setTimeout(tick, 40);
    };
    tick();
  })`);
  const wide = await evaluate(PAGE);
  async function shoot(name, theme, id) {
    if (!shotDir) return;
    await evaluate(prepare(theme, id));
    await sleep(60);
    const shot = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
    fs.writeFileSync(shotDir + '/' + name + '.png', Buffer.from(shot.data, 'base64'));
  }
  await shoot('wide-light-open', 'light', '3');
  await shoot('wide-dark-open', 'dark', '3');
  await send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: true,
  }, sessionId);
  await sleep(80);
  const narrow = await evaluate(`(function () {
    document.documentElement.setAttribute('data-theme', 'light');
    const cards = document.querySelectorAll('#kanban .kcard');
    let el = null;
    for (let i = 0; i < cards.length; i += 1) if (cards[i].dataset.task === '3') el = cards[i];
    document.getElementById('task-board').scrollIntoView({ block: 'start' });
    const rect = el.getBoundingClientRect();
    el.dispatchEvent(new PointerEvent('pointerover', {
      bubbles: true, cancelable: true, composed: true, pointerId: 1, pointerType: 'mouse',
      clientX: rect.left + 16, clientY: rect.top + 16,
    }));
    const link = document.querySelector('#kdeps .kdep-link');
    const path = document.querySelector('#kdeps .kdep');
    const box = path.getBBox();
    const svg = document.querySelector('#kdeps').getBoundingClientRect();
    const center = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    const end = document.querySelector('#kdeps .kdep-end').getBoundingClientRect();
    const endHit = document.elementFromPoint(end.left + end.width / 2, end.top + end.height / 2);
    return {
      side: link.getAttribute('data-dep-side'),
      target: link.getAttribute('data-dep-target'),
      open: link.getAttribute('data-dep-open'),
      count: document.querySelectorAll('#kdeps .kdep-link').length,
      box: { x: box.x, y: box.y, w: box.width, h: box.height },
      svgWidth: svg.width,
      centerInCard: !!(center && center.closest && center.closest('.kcard') === el),
      endInArc: !!(endHit && endHit.closest && endHit.closest('#kdeps')),
      light: getComputedStyle(path).stroke,
    };
  })()`);
  await shoot('narrow-light-stack', 'light', '3');
  await evaluate(`document.documentElement.setAttribute('data-theme', 'dark')`);
  const narrowDark = await evaluate(`getComputedStyle(document.querySelector('#kdeps .kdep.open')).stroke`);
  if (shotDir) {
    await sleep(40);
    const shot = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
    fs.writeFileSync(shotDir + '/narrow-dark-stack.png', Buffer.from(shot.data, 'base64'));
  }
  process.stdout.write(JSON.stringify({ wide: wide, narrow: narrow, narrowDark: narrowDark }));
  shutdown();
  process.exit(0);
})().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  shutdown();
  process.exit(1);
});
