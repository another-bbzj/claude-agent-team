/* DOM-independent UI boundaries; use synthetic messages, never personal logs. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'team-board', 'index.html'), 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));
new Function(script);
const start = script.indexOf('/* CODEX VIEW HELPERS */');
const end = script.indexOf('/* END CODEX VIEW HELPERS */');
assert(start >= 0 && end > start);
const box = {
  URLSearchParams,
  esc: value => String(value ?? '').replace(/[&<>\"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])),
  fmtClock: () => '12:00:00',
};
vm.createContext(box);
vm.runInContext(script.slice(start, end), box);
assert.equal(box.normalizeSource('codex'), 'codex');
assert.equal(box.normalizeSource('claude'), 'claude');
for (const bad of ['all', 'CODEX', null, 'invalid', '<script>']) assert.equal(box.normalizeSource(bad), 'all');
assert.equal(box.providerName({ session: { source: 'codex' } }), 'Codex');
assert.equal(box.providerName({ session: { id: 'legacy' } }), 'Claude Code');
assert.equal(box.leadName({ source: 'codex', lead: {} }), '队长 · Codex');
assert.equal(box.displayModel({ source: 'codex', model: 'gpt-6.1-sol', modelFamily: 'gpt' }), 'gpt-6.1-sol');
assert.equal(box.displayModel({ model: 'gpt-6-astra', modelFamily: 'gpt' }), 'gpt-6-astra');
assert.equal(box.displayModel({ modelFamily: 'sonnet', model: 'claude-sonnet-5' }), 'sonnet');
const url = new URL(box.snapshotUrl('codex', 'codex:thread?&id'), 'http://localhost');
assert.equal(url.pathname, '/snapshot.json');
assert.equal(url.searchParams.get('source'), 'codex');
assert.equal(url.searchParams.get('session'), 'codex:thread?&id');
assert.equal(box.selectableSessionId({ source: 'codex', id: 'shared-id' }), 'codex:shared-id');
assert.equal(box.selectableSessionId({ source: 'claude', id: 'shared-id' }), 'shared-id');
const rows = box.conversationRows({ conversation: [
  { role: 'system', text: 'private' }, { role: 'developer', text: 'private' },
  { role: 'tool', text: 'not dialogue' }, { role: 'assistant', text: '  ' },
  { role: 'assistant', text: '<img src=x onerror=alert(1)>', agentId: '<script>unsafe</script>', ts: 1 },
  { role: 'user', text: 'hello', ts: Infinity }, null,
] });
assert.equal(rows.length, 2);
assert.equal(rows[1].ts, 0);
const markup = box.conversationMarkup(rows, '<script>provider</script>');
assert(!markup.includes('<script>'));
assert(!markup.includes('<img'));
assert(markup.includes('&lt;img'));
assert.equal(box.conversationRows({}).length, 0);
const many = box.conversationRows({ conversation: Array.from({ length: 150 }, (_, i) => ({ role: 'assistant', text: String(i) })) });
assert.equal(many.length, 100);
assert.equal(many[0].text, '50');
assert.equal(box.conversationRows({ conversation: [{ role: 'user', text: 'x'.repeat(20000) }] })[0].text.length, 12000);
assert.equal(box.wallpaperFileError({ name: 'demo.mp4', type: 'video/mp4', size: 50 * 1024 * 1024 }), '');
assert.equal(box.wallpaperFileError({ name: 'demo.webm', type: '', size: 1 }), '');
assert.equal(box.wallpaperFileError({ name: 'bg.png', type: 'image/png', size: 15 * 1024 * 1024 }), '');
assert.match(box.wallpaperFileError({ name: 'demo.mp4', size: 50 * 1024 * 1024 + 1 }), /50 MB/);
assert.match(box.wallpaperFileError({ name: 'bg.png', size: 15 * 1024 * 1024 + 1 }), /15 MB/);
assert.match(box.wallpaperFileError({ name: 'project.html', type: 'text/html', size: 10 }), /只支持/);
console.log('Codex UI helpers: source isolation, model identity, dialogue escaping and wallpaper limits passed');

// Optional real-browser mode: node tests/check_codex_ui.js <pageUrl> <debugPort> [shotDir]
// The caller owns the isolated browser/profile and local page server lifecycle.
if (process.argv[2] && process.argv[3]) {
  const http = require('node:http');
  const pageUrl = process.argv[2], debugPort = process.argv[3], shotDir = process.argv[4] || '';
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const getJson = url => new Promise((resolve, reject) => {
    const req = http.get(url, res => { const chunks = []; res.on('data', x => chunks.push(x)); res.on('end', () => { try { resolve(JSON.parse(Buffer.concat(chunks))); } catch (error) { reject(error); } }); }); req.on('error', reject);
  });
  let socket;
  (async () => {
    const info = await getJson('http://127.0.0.1:' + debugPort + '/json/version');
    socket = new WebSocket(info.webSocketDebuggerUrl);
    const waiting = new Map(); let sequence = 0;
    const opened = new Promise((resolve, reject) => { socket.addEventListener('open', resolve); socket.addEventListener('error', reject); });
    socket.addEventListener('message', event => { const answer = JSON.parse(event.data); const request = waiting.get(answer.id); if (!request) return; waiting.delete(answer.id); answer.error ? request.reject(new Error(JSON.stringify(answer.error))) : request.resolve(answer.result); });
    await opened;
    const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => { const id = ++sequence; waiting.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) })); });
    const target = await send('Target.createTarget', { url: 'about:blank' });
    const attached = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
    const sessionId = attached.sessionId;
    const evaluate = async expression => { const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sessionId); if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails)); return result.result.value; };
    await send('Page.enable', {}, sessionId); await send('Runtime.enable', {}, sessionId);
    await send('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion',value:'no-preference'}]}, sessionId);
    const base = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'examples', 'board-demo.synthetic.json'), 'utf8'));
    const memberTemplate = structuredClone(base.members[0]);
    base.theaters = []; base.members = []; base.feed = []; base.comms = []; base.officialTeam = { available: false, tasks: [] }; base.live = { events: [], enabled: false };
    base.lead = { ...base.lead, name: 'Codex', phase: 'idle', model: 'gpt-6.1-sol', modelFamily: 'gpt', running: 0, done: 0, total: 0, current: null, timeline: [], active: false, lastUserPrompt: 'Synthetic conversation' };
    base.source = 'codex'; base.session = { id: 'synthetic-codex', source: 'codex', project: 'synthetic-project', lastActivity: Date.now() / 1000 };
    base.petInfo = base.petInfo || {};
    base.pets = base.pets || [];
    for (const id of ['aurelia-engineer', 'lyra-designer']) {
      const meta = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'team-board', 'sprites', id + '.json'), 'utf8'));
      const dimensions = id === 'aurelia-engineer' ? [1028, 1530] : [1136, 1385];
      base.petInfo[id] = {...meta,file:id+'.png',w:dimensions[0],h:dimensions[1]};
      base.pets.push(id);
    }
    base.lead.pet = 'aurelia-engineer';
    base.sessions = [{ id: 'synthetic-codex', source: 'codex', project: 'synthetic-project', count: 0, mtime: Date.now() / 1000 }];
    base.conversation = [{ role: 'user', text: 'Show a plain-text <img src=x onerror=alert(1)>', ts: 1 }, { role: 'assistant', text: 'Synthetic Codex response: ' + 'long-name/'.repeat(80), ts: 2, agentId: '__lead' }];
    await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => { const snapshot = ${JSON.stringify(base)}; const original = window.fetch.bind(window); window.__uiRequests = []; window.__imports = []; window.fetch = async (input, options) => { const url = new URL(String(input), location.href); if (url.pathname === '/snapshot.json') { window.__uiRequests.push(url.search); const copy = structuredClone(snapshot); if (url.searchParams.get('source') === 'claude') { copy.source = 'claude'; copy.session.source = 'claude'; copy.session.id = 'synthetic-claude'; copy.lead.name = 'Claude'; copy.conversation = [{role:'assistant',text:'Claude source response',ts:3}]; copy.sessions = []; } return new Response(JSON.stringify(copy), { headers: { 'content-type': 'application/json' } }); } if (url.pathname === '/api/wallpapers') return new Response(JSON.stringify({items:[],found:false,desktop:{available:false}}), {headers:{'content-type':'application/json'}}); if (url.pathname === '/api/backdrop' && (!options || options.method !== 'POST')) return new Response(JSON.stringify(snapshot.backdrop || {enabled:false,url:''}), {headers:{'content-type':'application/json'}}); if (url.pathname === '/api/backdrop' && options && options.method === 'POST') { window.__imports.push(JSON.parse(options.body)); return new Response(JSON.stringify({...snapshot.backdrop,enabled:false,url:'',note:'Synthetic project preview imported'}), {headers:{'content-type':'application/json'}}); } return original(input, options); }; })();` }, sessionId);
    await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 720, deviceScaleFactor: 1, mobile: false }, sessionId);
    await send('Page.navigate', { url: pageUrl }, sessionId);
    await evaluate(`new Promise((resolve,reject) => { const begun=Date.now(); const wait=()=>{if(document.querySelector('.person.lead .name')?.textContent.includes('Codex'))resolve(true);else if(Date.now()-begun>8000)reject(new Error('Codex board not rendered'));else setTimeout(wait,40)};wait();})`);
    const wide = await evaluate(`({name:document.querySelector('.person.lead .name').textContent,model:document.querySelector('.person.lead .chip').textContent,source:document.querySelector('#conversation-source').textContent,rows:document.querySelectorAll('.conversation-message').length,unsafe:document.querySelector('#conversation-list img')!==null})`);
    assert(wide.name.includes('Codex')); assert(wide.model.includes('gpt-6.1-sol')); assert.equal(wide.rows, 2); assert.equal(wide.unsafe, false);
    assert.equal(await evaluate(`document.querySelector('#comms-send').disabled`), true);
    await pause(400);
    assert.equal(await evaluate(`(() => {const c=document.querySelector('.person.lead .sprite canvas');return !!c && c.getContext('2d').getImageData(0,0,c.width,c.height).data.some((v,i)=>i%4===3&&v>0)})()`), true);
    assert((await evaluate(`document.querySelector('#composer-note').textContent`)).includes('只读'));
    const compact = await evaluate(`(() => {
      const saved=structuredClone(prev);
      prev.members=Array.from({length:7},(_,i)=>({...${JSON.stringify(memberTemplate)},id:'compact-'+i,name:'Member '+i,agentType:'codex-agent',pet:i===0?'drip':'',model:'gpt-6.1-sol',source:'codex'}));
      prev.departments=prev.departments.map(d=>({...d,members:d.id==='dev'?prev.members.map(m=>m.id):[]}));
      layout(prev);
      const rows={};for(const p of positions.values())rows[p.top]=(rows[p.top]||0)+1;
      const result={rows:Object.values(rows),pets:prev.members.map(m=>petOf(m)),collapsed:stallCollapsed(),heights:prev.members.map(m=>personEls.get(m.id).offsetHeight)};
      layout(saved);prev=saved;return result;
    })()`);
    assert.deepEqual(compact.rows,[4,3]);
    assert(compact.pets.every(p=>['aurelia-engineer','lyra-designer'].includes(p)));
    assert.equal(compact.collapsed,true);
    assert(compact.heights.every(h=>h<=256), JSON.stringify(compact));
    await evaluate(`document.querySelector('#source-filter').value='claude';document.querySelector('#source-filter').dispatchEvent(new Event('change',{bubbles:true}));`);
    await pause(150);
    assert.equal(await evaluate(`document.querySelector('#conversation-source').textContent`), 'Claude Code');
    assert.equal(await evaluate(`document.querySelector('#comms-send').disabled`), false);
    assert((await evaluate(`document.querySelector('#conversation-list').textContent`)).includes('Claude source response'));
    await evaluate(`document.querySelector('#source-filter').value='codex';document.querySelector('#source-filter').dispatchEvent(new Event('change',{bubbles:true}));`);
    await pause(150);
    const reports = [];
    assert.equal(await evaluate(`document.querySelector('main .conversation-panel') === null && document.querySelector('#conversation-modal').hidden`), true);
    await send('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion',value:'reduce'}]}, sessionId);
    await evaluate(`document.querySelector('#look-btn').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:20,clientY:20,pointerType:'mouse'}));`);
    assert.equal(await evaluate(`document.querySelector('#look-btn').classList.contains('rm-tap')`), true);
    assert.equal(await evaluate(`document.querySelectorAll('#look-btn .rpl-host').length`), 0);
    await send('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion',value:'no-preference'}]}, sessionId);
    await evaluate(`document.querySelector('#look-btn').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:20,clientY:20,pointerType:'mouse'}));`);
    assert.equal(await evaluate(`document.querySelectorAll('#look-btn .rpl-host').length`), 1);
    for (const [width, height, theme] of [[1280, 720, 'light'], [390, 650, 'light'], [390, 650, 'dark']]) {
      await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false }, sessionId);
      await evaluate(`document.documentElement.dataset.theme=${JSON.stringify(theme)};document.querySelector('#session-pill').click();`);
      await pause(400);
      if (shotDir) { fs.mkdirSync(shotDir, { recursive: true }); const shot = await send('Page.captureScreenshot', { format: 'png' }, sessionId); fs.writeFileSync(path.join(shotDir, 'codex-conversation-' + width + '-' + theme + '.png'), Buffer.from(shot.data, 'base64')); }
      await evaluate(`document.querySelector('#conversation-close').click();`);
      await pause(250);
      assert.equal(await evaluate(`document.querySelector('#conversation-modal').hidden`), true);
      await evaluate(`document.querySelector('#look-btn').click();`);
      await pause(500);
      const bounds = await evaluate(`(() => { const modal=document.querySelector('#look-modal .modal-card'), button=document.querySelector('#lk-done'), r=button.getBoundingClientRect(); return {width:innerWidth,height:innerHeight,pageOverflow:document.documentElement.scrollWidth>innerWidth+1,cardScroll:modal.scrollHeight>modal.clientHeight+1,clip:getComputedStyle(modal).clipPath,bottom:r.bottom,top:r.top,left:r.left,right:r.right};})()`);
      assert.equal(bounds.pageOverflow, false, JSON.stringify(bounds)); assert.equal(bounds.cardScroll, false, JSON.stringify(bounds)); assert(bounds.clip.startsWith('polygon('), bounds.clip); assert(bounds.top >= 0 && bounds.bottom <= height && bounds.left >= 0 && bounds.right <= width, JSON.stringify(bounds));
      if (shotDir) { fs.mkdirSync(shotDir, { recursive: true }); const shot = await send('Page.captureScreenshot', { format: 'png' }, sessionId); fs.writeFileSync(path.join(shotDir, 'codex-appearance-' + width + '-' + theme + '.png'), Buffer.from(shot.data, 'base64')); }
      reports.push(bounds);
      await evaluate(`document.querySelector('#lk-path').value='/synthetic/wallpaper-project';document.querySelector('#lk-path-go').click();`);
      await pause(120);
      assert((await evaluate(`document.querySelector('#lk-msg').textContent`)).includes('Synthetic project preview imported'));
      await evaluate(`document.querySelector('#lk-done').click();`);
      assert.equal(await evaluate(`document.querySelector('#look-modal').hidden`), true);
    }
    assert((await evaluate(`window.__imports`)).every(x => x.path === '/synthetic/wallpaper-project'));
    assert((await evaluate(`window.__uiRequests`)).some(x => x.includes('source=codex')));
    console.log(JSON.stringify({ browser: 'passed', wide, viewports: reports }));
    await send('Target.closeTarget', { targetId: target.targetId }); socket.close();
  })().catch(error => { console.error(error.stack || error); if (socket) socket.close(); process.exitCode = 1; });
}
