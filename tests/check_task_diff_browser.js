'use strict';
/* Headless check that task cards and tickets show git diff counts.

   node check_task_diff_browser.js <pageUrl> <debugPort> [shotDir]
   Chrome is already listening on debugPort.
*/
const http = require('http');
const fs = require('fs');

const pageUrl = process.argv[2];
const debugPort = process.argv[3];
const shotDir = process.argv[4] || '';

if (!pageUrl || !debugPort) {
  console.error('usage: node check_task_diff_browser.js <pageUrl> <debugPort> [shotDir]');
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

function connect(url) {
  const ws = new WebSocket(url);
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
  return ready.then(() => ({ send: send, close: () => { try { ws.close(); } catch (err) { /* already closed */ } } }));
}

function cardText(id) {
  return `(function () {
    const cards = document.querySelectorAll('#kanban .kcard');
    for (let i = 0; i < cards.length; i += 1) {
      if (cards[i].dataset.task === ${JSON.stringify(id)}) return cards[i].textContent;
    }
    return '';
  })()`;
}

function openCard(id) {
  return `(function () {
    const cards = document.querySelectorAll('#kanban .kcard');
    let el = null;
    for (let i = 0; i < cards.length; i += 1) if (cards[i].dataset.task === ${JSON.stringify(id)}) el = cards[i];
    if (!el) return '';
    el.scrollIntoView({ block: 'center', inline: 'nearest' });
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    const root = document.querySelector('#task-detail');
    const body = document.querySelector('#task-detail-body');
    return JSON.stringify({
      open: !!(root && !root.hidden),
      body: body ? body.textContent : '',
    });
  })()`;
}

async function main() {
  const version = await waitJson('http://127.0.0.1:' + debugPort + '/json/version');
  const browser = await connect(version.webSocketDebuggerUrl);
  const send = browser.send;
  try {
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
    async function shot(name) {
      if (!shotDir) return;
      await evaluate(`document.getElementById('task-board').scrollIntoView({block:'start'});`);
      await sleep(40);
      const image = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
      fs.writeFileSync(shotDir + '/' + name, Buffer.from(image.data, 'base64'));
    }
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1280, height: 900, deviceScaleFactor: 1, mobile: false,
    }, sessionId);
    await send('Page.navigate', { url: pageUrl }, sessionId);
    await evaluate(`new Promise((resolve, reject) => {
      const start = Date.now();
      const tick = () => {
        const cards = document.querySelectorAll('#kanban .kcard');
        const tickets = document.querySelector('#ticket-row');
        if (cards.length >= 5 && tickets && !tickets.hidden) resolve(true);
        else if (Date.now() - start > 8000) reject(new Error('board not ready'));
        else setTimeout(tick, 40);
      };
      tick();
    })`);
    await evaluate(`document.documentElement.setAttribute('data-theme', 'light')`);
    const card1 = await evaluate(cardText('1'));
    const card2 = await evaluate(cardText('2'));
    const card3 = await evaluate(cardText('3'));
    const tickets = await evaluate(`(document.querySelector('#ticket-row') || {}).textContent || ''`);
    await shot('task-diff-light.png');
    const drawer1 = JSON.parse(await evaluate(openCard('1')));
    await shot('task-diff-drawer.png');
    const drawer3 = JSON.parse(await evaluate(openCard('3')));
    await send('Emulation.setDeviceMetricsOverride', {
      width: 390, height: 844, deviceScaleFactor: 1, mobile: true,
    }, sessionId);
    await evaluate(`document.documentElement.setAttribute('data-theme', 'dark')`);
    await sleep(80);
    const narrow = await evaluate(cardText('1'));
    const narrowTickets = await evaluate(`(document.querySelector('#ticket-row') || {}).textContent || ''`);
    await shot('task-diff-narrow-dark.png');
    if (!drawer1.open || !drawer3.open) throw new Error('drawer did not open');
    process.stdout.write(JSON.stringify({
      card1: card1, card2: card2, card3: card3, tickets: tickets,
      drawer1: drawer1.body, drawer3: drawer3.body,
      narrow: narrow, narrowTickets: narrowTickets,
    }));
  } finally {
    browser.close();
  }
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
