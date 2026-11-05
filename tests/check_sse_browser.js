/* Browser check for optional loopback SSE. The caller starts Chrome and the page. */
'use strict';
const http = require('node:http');

const pageUrl = process.argv[2];
const debugPort = process.argv[3];
const mode = process.argv[4] || 'live';
if (!pageUrl || !debugPort) {
  console.error('usage: node tests/check_sse_browser.js <pageUrl> <debugPort> <live|fallback>');
  process.exit(2);
}

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const getJson = url => new Promise((resolve, reject) => {
  const req = http.get(url, res => {
    const chunks = [];
    res.on('data', chunk => chunks.push(chunk));
    res.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch (error) { reject(error); }
    });
  });
  req.on('error', reject);
});

(async () => {
  const info = await getJson('http://127.0.0.1:' + debugPort + '/json/version');
  const socket = new WebSocket(info.webSocketDebuggerUrl);
  const waiting = new Map();
  let sequence = 0;
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve);
    socket.addEventListener('error', reject);
  });
  socket.addEventListener('message', event => {
    const answer = JSON.parse(event.data);
    const request = waiting.get(answer.id);
    if (!request) return;
    waiting.delete(answer.id);
    if (answer.error) request.reject(new Error(JSON.stringify(answer.error)));
    else request.resolve(answer.result);
  });
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const id = ++sequence;
    waiting.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  const target = await send('Target.createTarget', { url: 'about:blank' });
  const attached = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
  const sessionId = attached.sessionId;
  const evaluate = async expression => {
    const result = await send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
    }, sessionId);
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: `(() => { window.__snapFetches = 0; const orig = window.fetch.bind(window); window.fetch = function(input, init) { try { const url = new URL(String(input), location.href); if (url.pathname === '/snapshot.json') window.__snapFetches++; } catch (e) {} return orig(input, init); }; })();`,
  }, sessionId);
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1280, height: 800, deviceScaleFactor: 1, mobile: false,
  }, sessionId);
  await send('Page.navigate', { url: pageUrl }, sessionId);

  const readLive = `(() => {
    try {
      return {
        ready: true,
        events: typeof liveEvents === 'undefined' ? null : liveEvents,
        poll: typeof pollTimer === 'undefined' ? null : pollTimer,
        hello: typeof sseHadHello === 'undefined' ? false : sseHadHello,
        text: document.body ? document.body.innerText : '',
        persons: document.querySelectorAll('.person').length,
        fetches: window.__snapFetches || 0,
      };
    } catch (e) {
      return { ready: false };
    }
  })()`;
  if (mode === 'fallback') {
    const state = await evaluate(`new Promise((resolve, reject) => {
      const begun = Date.now();
      const wait = () => {
        const snap = ${readLive};
        if (snap.ready && snap.persons > 0 && snap.poll != null && snap.events == null && snap.hello === false) {
          resolve({ persons: snap.persons, poll: true, closed: true, hello: false, fetches: snap.fetches });
          return;
        }
        if (Date.now() - begun > 8000) {
          reject(new Error('fallback not reached ' + JSON.stringify(snap)));
          return;
        }
        setTimeout(wait, 40);
      };
      wait();
    })`);
    process.stdout.write(JSON.stringify({ mode, state }));
    socket.close();
    return;
  }

  const opened = await evaluate(`new Promise((resolve, reject) => {
    const begun = Date.now();
    const wait = () => {
      const snap = ${readLive};
      const events = snap.events;
      const ready = snap.ready && events && events.readyState === 1 && snap.hello === true && snap.poll == null && snap.text.includes('阿服');
      if (ready) {
        resolve({ url: events.url, hello: true, poll: false, fetches: snap.fetches });
        return;
      }
      if (Date.now() - begun > 8000) {
        reject(new Error('sse not open ' + JSON.stringify({
          ready: snap.ready, url: events && events.url, state: events && events.readyState,
          hello: snap.hello, poll: snap.poll != null, hasName: !!(snap.text && snap.text.includes('阿服')),
        })));
        return;
      }
      setTimeout(wait, 40);
    };
    wait();
  })`);
  const settled = await evaluate(`new Promise(resolve => {
    setTimeout(() => resolve(window.__snapFetches), 250);
  })`);
  const later = await evaluate(`new Promise(resolve => {
    setTimeout(() => resolve({ fetches: window.__snapFetches, poll: pollTimer != null, hello: sseHadHello, url: liveEvents && liveEvents.url }), 1700);
  })`);
  await evaluate(`selectSession('sess-0001')`);
  const switched = await evaluate(`new Promise((resolve, reject) => {
    const begun = Date.now();
    const wait = () => {
      let url = '', hello = false, polling = true, ready = false;
      try {
        ready = true;
        url = (typeof liveEvents !== 'undefined' && liveEvents && liveEvents.url) || '';
        hello = typeof sseHadHello !== 'undefined' && sseHadHello;
        polling = typeof pollTimer !== 'undefined' && pollTimer != null;
      } catch (e) {
        ready = false;
      }
      if (ready && hello && !polling && url.includes('session=sess-0001')) {
        resolve({ url, hello: true, poll: false });
        return;
      }
      if (Date.now() - begun > 8000) {
        reject(new Error('session stream not replaced ' + JSON.stringify({ url, hello, poll: polling, ready })));
        return;
      }
      setTimeout(wait, 40);
    };
    wait();
  })`);
  process.stdout.write(JSON.stringify({
    mode,
    opened,
    settled,
    later,
    switched,
    pollStopped: later.fetches === settled && later.poll === false,
  }));
  socket.close();
})().catch(error => {
  console.error(error && error.stack || error);
  process.exit(1);
});
