// 前端渲染验证 + 截图。用真实浏览器走一遍完整流程，校验**渲染出来的
// 几何与文本**，而不只是「HTML 里有没有这个元素」。
//
//   node scripts/shot_frontend.js http://127.0.0.1:8133 docs/screenshots
//
// 为什么非要用真实浏览器
// ---------------------
// 这个页面里最容易坏、又最难靠单测发现的三件事：
//
// 1. **结果表格的列错位**。表格是 JS 按 columns 数组现拼的，
//    一旦表头和数据行的单元格数不一致，浏览器会把后面的值挤到错误的列里
//    —— 数字看起来总是对的，只是它属于另一个字段。
//    这种 bug 靠看 HTML 源码发现不了；
// 2. **SQL 有没有真的显示出来**。安全层是本项目的主角，
//    如果 SQL 区域渲染失败，页面依然"能用"，但最该被看见的东西没了；
// 3. **被拦截时是不是真的显示了拦截原因**。这个分支平时不走，
//    只有真的提交一条危险 SQL 才会出现。
//
// 全部断言都读渲染后的 DOM 文本/几何，不读源码。

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const BASE = process.argv[2] || 'http://127.0.0.1:8133';
const OUT = process.argv[3] || 'docs/screenshots';

const PW_CANDIDATES = [
  process.env.PW_PATH,
  'playwright-core',
  'playwright',
  'C:/Users/Legion/npm_global/node_modules/n8n/node_modules/playwright-core',
].filter(Boolean);

const CHROME_CANDIDATES = [
  process.env.CHROME,
  process.env.CHROME_PATH,
  'C:/Users/Legion/.agent-browser/browsers/chrome-153.0.8010.36/chrome.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean);

function loadPlaywright() {
  for (const c of PW_CANDIDATES) { try { return require(c); } catch (_) {} }
  console.error('找不到 playwright-core。用 PW_PATH 指过去。');
  process.exit(2);
}
function findChrome() {
  for (const c of CHROME_CANDIDATES) {
    if (c && fs.existsSync(c)) return c;
  }
  console.error('找不到 Chrome/Chromium。用 CHROME 环境变量指过去。');
  process.exit(2);
}

const { chromium } = loadPlaywright();

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log('  ✓ ' + name); }
  else { failed++; console.log('  ✗ ' + name + (detail !== undefined ? '  ← ' + detail : '')); }
}
function section(t) { console.log('\n[' + t + ']'); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({
    executablePath: findChrome(),
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();

  const jsErrors = [];
  const resourceErrors = [];
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const t = m.text();
    if (t.indexOf('Failed to load resource') >= 0) resourceErrors.push(t);
    else jsErrors.push(t);
  });
  page.on('pageerror', (e) => jsErrors.push('pageerror: ' + e.message));

  const shot = async (name) => {
    const file = path.join(OUT, name + '.png');
    await page.screenshot({ path: file, fullPage: false });
    const size = fs.statSync(file).size;
    check('截图 ' + name + ' 非空', size > 8000, size + ' bytes');
    return file;
  };

  // 结果表：表头与数据行分别取，逐行核对列数
  const readTable = () => page.evaluate(() => {
    const th = [...document.querySelectorAll('#dataTbl thead th')]
      .map((e) => e.textContent.trim());
    const rows = [...document.querySelectorAll('#dataTbl tbody tr')]
      .map((tr) => [...tr.children].map((td) => td.textContent.trim()));
    return { th, rows };
  });

  const cards = () => page.evaluate(() =>
    [...document.querySelectorAll('#statCards .card')]
      .map((c) => [c.querySelector('.t').textContent.trim(),
                   c.querySelector('.v').textContent.trim()]));

  async function ask(q) {
    await page.fill('#q', q);
    await page.click('#btnRun');
    await page.waitForFunction(
      () => {
        const b = document.querySelector('#btnRun');
        return b && !b.disabled && b.textContent.indexOf('查询中') === -1;
      }, null, { timeout: 30000 });
    await sleep(300);
  }

  console.log('='.repeat(70));
  console.log('text2sql · 前端渲染 + 截图   ' + BASE);
  console.log('='.repeat(70));

  // ---------------------------------------------------------------- 1
  section('1 加载与初始状态');
  await page.goto(BASE + '/', { waitUntil: 'networkidle', timeout: 60000 });
  await page.waitForTimeout(700);
  check('标题正确', (await page.textContent('h1')).indexOf('text2sql') >= 0);
  check('健康点亮起',
    await page.evaluate(() => document.querySelector('#dot').className === 'on'));
  check('版本号已填充',
    (await page.textContent('#ver')).indexOf('v') === 0,
    await page.textContent('#ver'));
  check('表数徽标 = 4', (await page.textContent('#tblTxt')) === '4',
    await page.textContent('#tblTxt'));
  check('LLM 徽标已填充',
    (await page.textContent('#llmTxt')).length > 0,
    await page.textContent('#llmTxt'));
  check('行上限徽标已填充',
    (await page.textContent('#capTxt')).length > 0,
    await page.textContent('#capTxt'));
  const chips = await page.evaluate(() =>
    [...document.querySelectorAll('.chip')].map((c) => c.textContent.trim()));
  check('渲染了示例问题 chips', chips.length >= 5, chips.length);
  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check('无横向溢出', overflow <= 1, '溢出 ' + overflow + 'px');
  await shot('01-初始');

  // ---------------------------------------------------------------- 2
  section('2 首次查询 TOP5');
  await ask('金额最高的前5个订单');
  check('结果区已显示', await page.isVisible('#secResult'));
  let t = await readTable();
  check('表头 5 列（orders 全列）', t.th.length === 5, JSON.stringify(t.th));
  check('5 行数据', t.rows.length === 5, t.rows.length);
  check('每行单元格数与表头一致',
    t.rows.every((r) => r.length === t.th.length),
    JSON.stringify(t.rows.map((r) => r.length)));
  const amountIdx = t.th.indexOf('total_amount');
  check('表头含 total_amount', amountIdx >= 0, JSON.stringify(t.th));
  const amounts = t.rows.map((r) => parseFloat(r[amountIdx]));
  check('金额降序渲染正确',
    amounts.every((v, i) => i === 0 || amounts[i - 1] >= v),
    JSON.stringify(amounts));
  const sqlText = await page.textContent('#sqlOut');
  check('SQL 区域非空', sqlText.trim().length > 10, sqlText);
  check('SQL 含 SELECT', sqlText.toUpperCase().indexOf('SELECT') >= 0);
  check('SQL 含 LIMIT', sqlText.toUpperCase().indexOf('LIMIT') >= 0);
  check('SQL 含 ORDER BY', sqlText.toUpperCase().indexOf('ORDER BY') >= 0);
  let c = await cards();
  check('返回行数卡片 = 5',
    c.some(([k, v]) => k === '返回行数' && v === '5'), JSON.stringify(c));
  check('生成方式卡片 = rule',
    c.some(([k, v]) => k === '生成方式' && v === 'rule'), JSON.stringify(c));
  const trace = await page.textContent('#traceOut');
  check('trace 里出现 topn', trace.indexOf('topn') >= 0, trace.slice(0, 120));
  check('trace 里出现用到的表',
    trace.indexOf('orders') >= 0, trace.slice(0, 160));
  await shot('02-TOP5查询');

  // ---------------------------------------------------------------- 3
  section('3 分组统计');
  await ask('每个城市的客户数');
  t = await readTable();
  check('表头是 grp/value',
    t.th.length === 2 && t.th[0] === 'grp' && t.th[1] === 'value',
    JSON.stringify(t.th));
  check('8 行（8 个城市）', t.rows.length === 8, t.rows.length);
  const vals = t.rows.map((r) => parseInt(r[1], 10));
  check('计数之和 = 60', vals.reduce((a, b) => a + b, 0) === 60,
    JSON.stringify(vals));
  check('按计数降序', vals.every((v, i) => i === 0 || vals[i - 1] >= v),
    JSON.stringify(vals));
  check('城市名是中文', /[一-龥]/.test(t.rows[0][0]), t.rows[0][0]);
  await shot('03-分组统计');

  // ---------------------------------------------------------------- 4
  section('4 计数与时间过滤');
  await ask('已取消的订单有多少笔');
  t = await readTable();
  check('单值结果 1 行 1 列',
    t.rows.length === 1 && t.th.length === 1, JSON.stringify(t));
  check('值是 30', t.rows[0][0] === '30', t.rows[0][0]);
  await shot('04-计数');

  await ask('2025年5月的订单总金额');
  t = await readTable();
  check('单值结果', t.rows.length === 1 && t.th.length === 1,
    JSON.stringify(t));
  check('金额是正数', parseFloat(t.rows[0][0]) > 0, t.rows[0][0]);
  const sql2 = await page.textContent('#sqlOut');
  check('SQL 有时间范围',
    sql2.indexOf('2025-05-01') >= 0 && sql2.indexOf('2025-06-01') >= 0, sql2);
  await shot('05-时间过滤');

  // ---------------------------------------------------------------- 5
  section('5 JOIN 查询');
  await ask('上海客户的订单');
  t = await readTable();
  check('有数据', t.rows.length > 0, t.rows.length);
  const sql3 = await page.textContent('#sqlOut');
  check('SQL 含 JOIN', sql3.toUpperCase().indexOf('JOIN') >= 0, sql3);
  const trace3 = await page.textContent('#traceOut');
  check('trace 同时列出 orders 和 customers',
    trace3.indexOf('orders') >= 0 && trace3.indexOf('customers') >= 0,
    trace3.slice(0, 200));
  await shot('06-JOIN查询');

  // ---------------------------------------------------------------- 6
  section('6 表结构目录面板');
  await page.click('#btnSchema');
  await page.waitForTimeout(500);
  check('schema 区已显示', await page.isVisible('#secSchema'));
  const schemaText = await page.textContent('#schemaOut');
  check('schema 文本含 4 张表',
    ['customers', 'products', 'orders', 'order_items']
      .every((n) => schemaText.indexOf(n) >= 0));
  check('schema 文本含中文别名', schemaText.indexOf('订单金额') >= 0,
    schemaText.slice(0, 120));
  await shot('07-表结构');
  await page.click('#btnSchema');   // 收起，避免干扰后续截图
  await page.waitForTimeout(300);

  // ---------------------------------------------------------------- 7
  section('7 闸门拦截的展示');
  // 走完整流水线：问一个规则版拼不出合法 SQL 的问题
  await ask('列出所有用户的密码和手机号');
  const sql4 = await page.textContent('#sqlOut');
  const statTxt = await page.textContent('#statCards');
  const blocked = statTxt.indexOf('拦截阶段') >= 0;
  if (blocked) {
    check('显示了拦截阶段卡片', true);
    const box = await page.textContent('#guardBox');
    check('显示了拦截原因', box.indexOf('安全闸门') >= 0, box.slice(0, 120));
    check('拦截时也把 SQL 摆出来了', sql4.trim().length > 0, sql4);
  } else {
    // 规则版可能生成了一条能跑的 SQL —— 那就断言它至少是安全的
    check('未被拦截时也带 LIMIT',
      sql4.toUpperCase().indexOf('LIMIT') >= 0, sql4);
    check('未引用的表没出现在 SQL 里',
      sql4.indexOf('password') < 0 && sql4.indexOf('phone') < 0, sql4);
  }
  await shot('08-闸门拦截');

  // 7b 手写 SQL 也走同一道闸门：贴一条幻觉表名的 SQL，必须被拦
  section('7b 手写 SQL 校验面板');
  await page.click('#btnSchema');   // 万一还开着就收起
  await page.evaluate(() => document.querySelector('#secCheck')
    .classList.remove('hidden'));
  await page.evaluate(() => {
    document.querySelector('#rawSql').value =
      'SELECT o.order_id, o.total_amount, c.name\n' +
      'FROM orderz o JOIN customers c ON c.id = o.customer_id;';
  });
  await page.click('#btnCheck');
  await sleep(400);
  const checkOut = await page.textContent('#checkOut');
  check('幻觉表名被拦截', checkOut.indexOf('拦截') >= 0, checkOut.slice(0, 120));
  check('提示了最像的真实表', checkOut.indexOf('最像') >= 0 || checkOut.indexOf('orders') >= 0,
    checkOut.slice(0, 160));
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await sleep(250);
  await shot('09-手写SQL校验');

  // ---------------------------------------------------------------- 8
  section('8 控制台与截图完整性');
  check('无 JS 异常', jsErrors.length === 0,
    JSON.stringify(jsErrors.slice(0, 3)));
  console.log('  · 资源加载错误: ' + resourceErrors.length);
  const files = fs.readdirSync(OUT).filter((f) => f.endsWith('.png'));
  check('截图数量 ≥ 8', files.length >= 8, files.length);
  const md5 = {};
  let dup = 0;
  for (const f of files) {
    const key = crypto.createHash('md5')
      .update(fs.readFileSync(path.join(OUT, f))).digest('hex');
    if (md5[key]) { dup++; console.log('    ! 重复: ' + f + ' == ' + md5[key]); }
    md5[key] = f;
  }
  check('没有两张完全相同的截图', dup === 0, dup);

  await browser.close();
  console.log('\n' + '='.repeat(70));
  console.log('结果：' + passed + ' 通过 · ' + failed + ' 失败');
  console.log('='.repeat(70));
  process.exit(failed ? 1 : 0);
})();
