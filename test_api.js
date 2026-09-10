/* 端到端联调：起本地静态服务器 -> 真实 fetch 接口 -> eval 页面脚本 -> 检查渲染结果 */
const fs = require('fs');
const http = require('http');
const path = require('path');

const DIST = 'C:/Users/20194/WorkBuddy/2026-09-07-12-47-14/campus-jobs/dist';
const PORT = 8765;
const MIME = { '.html': 'text/html; charset=utf-8', '.json': 'application/json; charset=utf-8' };

const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split('?')[0]);
  if (p === '/') p = '/app.html';
  const f = path.join(DIST, p);
  if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); return res.end('404'); }
  res.writeHead(200, {
    'Content-Type': MIME[path.extname(f)] || 'application/octet-stream',
    'Access-Control-Allow-Origin': '*',
  });
  fs.createReadStream(f).pipe(res);
});

function mk(id) {
  if (store[id]) return store[id];
  const el = {
    id, value: '', checked: id === 'posOnly' || id === 'incNation',
    disabled: false, textContent: '', innerHTML: '',
    hidden: true, className: '', dataset: {}, style: {}, children: [],
    classList: { toggle() {}, contains: () => false, add() {}, remove() {} },
    addEventListener(t, fn) { (el._ev = el._ev || {})[t] = fn; },
    appendChild() {}, setAttribute() {}, removeAttribute() {},
    querySelectorAll: () => [], querySelector: () => null,
    parentElement: null, previousElementSibling: null, nextElementSibling: null,
  };
  return (store[id] = el);
}
const store = {};
const lsData = {};
global.localStorage = {
  getItem: k => (k in lsData ? lsData[k] : null),
  setItem: (k, v) => { lsData[k] = String(v); },
  removeItem: k => { delete lsData[k]; },
};
global.location = { search: `?api=http://127.0.0.1:${PORT}/api/` };
global.document = {
  getElementById: mk,
  createElement: () => mk('_tmp_' + Math.random()),
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener() {},
  activeElement: null,
};
global.window = { scrollTo() {}, addEventListener() {}, innerWidth: 1400, scrollY: 0 };

(async () => {
  await new Promise(r => server.listen(PORT, r));
  console.log(`静态服务器已启动 http://127.0.0.1:${PORT}\n`);

  const html = fs.readFileSync(path.join(DIST, 'app.html'), 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  console.log('脚本块数:', scripts.length, '| app.html 体积:', (html.length / 1024).toFixed(1), 'KB');

  // 所有 script 块必须在同一作用域里 eval，否则 const 声明跨块不可见
  const runAll = tag => {
    const logic = scripts[scripts.length - 1]
      .replace(/\nboot\(\);/, `\nglobalThis.${tag} = boot(); globalThis.loadAll = loadAll;`);
    eval(scripts.slice(0, -1).join('\n') + '\n' + logic);
    return globalThis[tag];
  };

  console.log('\n--- 阶段1：首屏（只拉物流分包）---');
  await runAll('__boot');
  const bar1 = store['apiBar'].innerHTML.replace(/<[^>]+>/g, '');
  console.log('状态条:', bar1);
  console.log('结果条数:', store['cnt'].textContent, '| 公司:', store['hsComp'].textContent, '| 城市:', store['hsCity'].textContent);
  const cards1 = (store['list'].innerHTML.match(/class="card"/g) || []).length;
  console.log('渲染卡片:', cards1, '| 高亮:', (store['list'].innerHTML.match(/<mark>/g) || []).length);
  console.log('hsAll(总收录):', store['hsAll'].textContent);
  console.log('localStorage 缓存条数:', lsData['jobs.rows'] ? JSON.parse(lsData['jobs.rows']).length : '未缓存');

  console.log('\n--- 阶段2：切换全量 ---');
  await globalThis.loadAll();
  const bar2 = store['apiBar'].innerHTML.replace(/<[^>]+>/g, '');
  console.log('状态条:', bar2);
  console.log('结果条数:', store['cnt'].textContent, '| 公司:', store['hsComp'].textContent);
  const cards2 = (store['list'].innerHTML.match(/class="card"/g) || []).length;
  console.log('渲染卡片:', cards2);

  console.log('\n--- 阶段3：模拟二次访问（应命中缓存，不再下载）---');
  const t0 = Date.now();
  await runAll('__boot2');
  console.log('耗时:', Date.now() - t0, 'ms（命中缓存应远快于首次）');
  console.log('状态条:', store['apiBar'].innerHTML.replace(/<[^>]+>/g, ''));
  console.log('结果条数:', store['cnt'].textContent);

  console.log('\n--- 阶段4：接口异常时的降级 ---');
  global.location = { search: '?api=http://127.0.0.1:1/none/' };
  await runAll('__boot3');
  const bar3 = store['apiBar'].innerHTML.replace(/<[^>]+>/g, '');
  console.log('状态条:', bar3);
  console.log('是否标记为错误态:', store['apiBar'].className.includes('err') ? '是' : '否');

  server.close();
  console.log('\n✅ 联调结束');
})().catch(e => { console.error('❌ 失败:', e.message); server.close(); process.exit(1); });
