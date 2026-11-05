'use strict';
/* Headless check for the read-only acceptance panel.

   node check_task_acceptance_browser.js <pageUrl> <debugPort> [shotDir]
   Chrome is already listening on debugPort. This script only drives the page.
*/
const http = require('http');
const fs = require('fs');

const pageUrl = process.argv[2];
const debugPort = process.argv[3];
const shotDir = process.argv[4] || '';

if (!pageUrl || !debugPort) {
  console.error('usage: node check_task_acceptance_browser.js <pageUrl> <debugPort> [shotDir]');
  process.exit(1);
}

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

let socket = null;
function closeSocket() {
  try { if (socket) socket.close(); } catch (err) { /* already closed */ }
  socket = null;
}

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
  function badge(id) {
    const mark = card(id).querySelector('.kaccept');
    return mark ? mark.textContent : '';
  }
  function open(id) {
    const el = card(id);
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return detail();
  }
  function detail() {
    const root = document.querySelector('#task-detail');
    const body = document.querySelector('#task-detail-body');
    const box = body.querySelector('.td-accept');
    return {
      open: !root.hidden,
      headings: Array.from(body.querySelectorAll('h3')).map((node) => node.textContent),
      text: body.textContent || '',
      checks: Array.from(body.querySelectorAll('.td-accept input')).map((input) => ({
        disabled: input.disabled,
        checked: input.checked,
      })),
      form: !!body.querySelector('form'),
      width: box ? box.getBoundingClientRect().width : 0,
    };
  }
  function clickBox() {
    const input = document.querySelector('#task-detail .td-accept input');
    const before = input.checked;
    input.click();
    return { checked: input.checked, stayed: input.checked === before };
  }
  document.documentElement.setAttribute('data-theme', 'light');
  document.getElementById('task-board').scrollIntoView({ block: 'start' });
  const badges = { '1': badge('1'), '2': badge('2'), '5': badge('5') };
  const listedDetail = open('3');
  const afterClick = clickBox();
  const listed = { badge: badge('3'), detail: listedDetail, afterClick: afterClick };
  const prose = { badge: badge('4'), detail: open('4') };
  const missing = { badge: badge('5'), detail: open('5') };
  const light = getComputedStyle(document.querySelector('#task-detail h3')).color;
  document.documentElement.setAttribute('data-theme', 'dark');
  const dark = getComputedStyle(document.querySelector('#task-detail h3')).color;
  document.documentElement.setAttribute('data-theme', 'light');
  return { listed: listed, prose: prose, missing: missing, badges: badges, light: light, dark: dark };
})()`;

(async function main() {
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
  async function shoot(name) {
    if (!shotDir) return;
    await sleep(40);
    const shot = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
    fs.writeFileSync(shotDir + '/' + name + '.png', Buffer.from(shot.data, 'base64'));
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
  await evaluate(`(function () {
    const cards = document.querySelectorAll('#kanban .kcard');
    for (let i = 0; i < cards.length; i += 1) {
      if (cards[i].dataset.task === '3') cards[i].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    }
    const box = document.querySelector('#task-detail .td-accept');
    if (box) box.scrollIntoView({ block: 'center' });
  })()`);
  await shoot('wide-light-acceptance');
  await evaluate(`document.documentElement.setAttribute('data-theme', 'dark')`);
  await shoot('wide-dark-acceptance');
  await send('Emulation.setDeviceMetricsOverride', {
    width: 390, height: 844, deviceScaleFactor: 1, mobile: true,
  }, sessionId);
  await sleep(80);
  const narrow = await evaluate(`(function () {
    document.documentElement.setAttribute('data-theme', 'light');
    const cards = document.querySelectorAll('#kanban .kcard');
    let el = null;
    for (let i = 0; i < cards.length; i += 1) if (cards[i].dataset.task === '3') el = cards[i];
    el.scrollIntoView({ block: 'center' });
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    const body = document.querySelector('#task-detail-body');
    const box = body.querySelector('.td-accept');
    return {
      open: !document.querySelector('#task-detail').hidden,
      headings: Array.from(body.querySelectorAll('h3')).map((node) => node.textContent),
      width: box ? box.getBoundingClientRect().width : 0,
    };
  })()`);
  await shoot('narrow-light-acceptance');
  process.stdout.write(JSON.stringify({
    listed: wide.listed,
    prose: wide.prose,
    missing: wide.missing,
    badges: wide.badges,
    light: wide.light,
    dark: wide.dark,
    narrow: narrow,
  }));
  closeSocket();
  process.exit(0);
})().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  closeSocket();
  process.exit(1);
});
