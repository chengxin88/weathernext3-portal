const fs = require('fs'), vm = require('vm'), assert = require('assert');
const script = fs.readFileSync(__dirname + '/static/index.html', 'utf8').split('<script>')[1].split('</script>')[0]
  .replace(/init\(\)\.catch\(error =>.*?\);\s*$/, '');
const elements = new Map(), events = [], timers = [], intervals = [];
const document = {hidden: false, activeElement: null,
  getElementById(id) {if (!elements.has(id)) elements.set(id, {textContent: '', hidden: false}); return elements.get(id);},
  addEventListener(...args) {events.push(args);}};
let response;
const context = vm.createContext({document, console, URLSearchParams, AbortSignal,
  fetch: async () => {if (response instanceof Error) throw response; return response;},
  setTimeout: (fn, ms) => {timers.push({fn, ms}); return timers.length;}, clearTimeout() {},
  setInterval: (fn, ms) => {intervals.push({fn, ms}); return intervals.length;}, clearInterval() {},
  location: {hash: ''}});
vm.runInContext(script + '\ndrawRuns = () => {}; update = () => {}; drawDownloadNote = () => {}; readHash = () => {};', context);
const run = code => vm.runInContext(code, context);
const valid = {runs: [{id: '2026100922'}], products: {rain1: 'rain'}, regions: {south: 'south'}};
const reply = (body, status=200, kind='application/json') => ({ok: status===200, status, headers: {get: () => kind}, json: async () => body});
(async () => {
  response = reply({runs: [], catalog_status: 'preparing'});
  await run('init()');
  assert.equal(intervals.length, 0); assert.equal(timers.length, 1);
  assert.match(elements.get('msg').textContent, /索引正在准备/);
  response = Object.assign(new Error('timeout'), {name: 'TimeoutError'});
  await run('init()'); assert.match(elements.get('msg').textContent, /超时/); assert.equal(timers.length, 2);
  response = reply({}, 401);
  await run('init()'); assert.equal(timers.length, 2); assert.match(elements.get('msg').textContent, /登录/);
  response = reply(valid);
  await run('init()'); await run('init()');
  assert.equal(intervals.length, 1); assert.equal(events.length, 2);
  response = reply({}, 403);
  await run('refreshCatalog()'); assert.match(elements.get('download-note').textContent, /登录/);
  response = reply({}, 503);
  await run('refreshCatalog()'); assert.match(elements.get('download-note').textContent, /自动重试/);
  response = reply({}, 200, 'text/html');
  await assert.rejects(run('fetchCatalog()'), /登录状态/);
  console.log('Frontend preparing/timeout/auth/retry/single-init tests passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
