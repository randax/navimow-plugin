import { chromium } from '@playwright/test';
const OUT = process.argv[2];
const base = 'http://localhost:3000';
const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const results = {};
for (const uid of ['terrain-3d', 'terrain-2d']) {
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 }, httpCredentials: { username: 'admin', password: 'admin' } });
  const page = await ctx.newPage();
  const console_ = []; const failed = []; const tiles = {};
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) console_.push(`${m.type()}: ${m.text()}`); });
  page.on('pageerror', (e) => console_.push(`pageerror: ${e.message}`));
  page.on('requestfailed', (r) => failed.push(`${r.url()} -> ${r.failure()?.errorText}`));
  page.on('response', (r) => { const h = new URL(r.url()).host; if (/kartverket|mapterhorn/.test(h)) { tiles[h] ??= {}; tiles[h][r.status()] = (tiles[h][r.status()] ?? 0) + 1; } });
  const t0 = Date.now();
  await page.goto(`${base}/d/${uid}?kiosk`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.maplibregl-canvas', { timeout: 30000 }).catch(() => console_.push('no .maplibregl-canvas within 30 s'));
  await page.waitForTimeout(12000);
  const panel = page.locator('[data-viz-panel-key], .panel-container, section').first();
  await page.screenshot({ path: `${OUT}/${uid}.png` });
  const attribution = await page.locator('.maplibregl-ctrl-attrib').innerText().catch(() => 'n/a');
  results[uid] = { ms_to_screenshot: Date.now() - t0, tiles, failed: [...new Set(failed)], console: [...new Set(console_)], attribution };
  await ctx.close();
}
await browser.close();
console.log(JSON.stringify(results, null, 1));
