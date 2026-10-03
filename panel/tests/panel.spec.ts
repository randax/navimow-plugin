import type { Page } from '@playwright/test';
import { test, expect } from '@grafana/plugin-e2e';

const PLUGIN_ID = 'randax-navimowmap-panel';
const dashboard = { uid: 'navimow-map' };

/** Console errors and uncaught exceptions that come from this plugin, not from Grafana itself. */
function collectPluginErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (m) => {
    if (m.type() === 'error' && (m.text().includes('[navimow-map]') || m.location().url.includes(PLUGIN_ID))) {
      errors.push(m.text());
    }
  });
  page.on('pageerror', (e) => {
    if (e.stack?.includes(PLUGIN_ID)) {
      errors.push(e.message);
    }
  });
  return errors;
}

const tileResponse = (page: Page, host: string) =>
  page.waitForResponse((r) => new URL(r.url()).host === host && r.status() === 200);

test('draws the default Kartverket Base map with its attribution and no errors of its own', async ({
  gotoDashboardPage,
  page,
}) => {
  const errors = collectPluginErrors(page);
  const tile = tileResponse(page, 'cache.kartverket.no');
  const dashboardPage = await gotoDashboardPage(dashboard);
  const panel = dashboardPage.getPanelByTitle('Kartverket topo').locator;

  await expect(panel.locator('canvas.maplibregl-canvas')).toBeVisible();
  await tile;
  const attribution = panel.locator('.maplibregl-ctrl-attrib');
  await expect(attribution).toContainText('© Kartverket');
  await expect(attribution).not.toHaveClass(/maplibregl-compact/);
  expect(errors).toEqual([]);
});

test('OpenStreetMap credits its contributors', async ({ gotoDashboardPage, page }) => {
  const tile = tileResponse(page, 'tile.openstreetmap.org');
  const dashboardPage = await gotoDashboardPage(dashboard);
  const panel = dashboardPage.getPanelByTitle('OpenStreetMap').locator;
  await tile;
  const attribution = panel.locator('.maplibregl-ctrl-attrib');
  await expect(attribution).toContainText('© OpenStreetMap contributors');
  // Plain-text credit must stay dark on the light attribution strip, whatever Grafana's theme.
  await expect(attribution.locator('.maplibregl-ctrl-attrib-inner')).toHaveCSS('color', 'rgba(0, 0, 0, 0.75)');
});

test('a custom WMS bounding-box template draws tiles from its own host', async ({ gotoDashboardPage, page }) => {
  const tile = tileResponse(page, 'wms.geonorge.no');
  const dashboardPage = await gotoDashboardPage(dashboard);
  const panel = dashboardPage.getPanelByTitle('Custom WMS').locator;
  await tile;
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© Kartverket');
});

test('a custom Base map without attribution shows why instead of a map', async ({ gotoDashboardPage }) => {
  const dashboardPage = await gotoDashboardPage(dashboard);
  const panel = dashboardPage.getPanelByTitle('Custom without attribution').locator;
  await expect(panel.getByTestId('navimow-map-message')).toContainText('needs an attribution');
  await expect(panel.locator('canvas')).toHaveCount(0);
});

test('the Base map picker offers the presets and switches the map', async ({ gotoPanelEditPage, page }) => {
  const panelEditPage = await gotoPanelEditPage({ dashboard, id: '1' });
  const picker = panelEditPage.getCustomOptions('Base map').getSelect('Base map');
  await picker.locator().getByRole('combobox').click();
  await expect(page.getByRole('option')).toHaveText([
    /^Kartverket topo/,
    /^Kartverket topo gråtone/,
    /^Kartverket turkart \(toporaster\)/,
    /^OpenStreetMap.*usage policy/,
    /^Custom/,
  ]);
  await page.keyboard.press('Escape');

  const tile = tileResponse(page, 'tile.openstreetmap.org');
  await picker.selectOption('OpenStreetMap');
  await tile;
  await expect(panelEditPage.panel.locator.locator('.maplibregl-ctrl-attrib')).toContainText('OpenStreetMap');
});

test('a browser without WebGL gets a readable message instead of a blank panel', async ({
  gotoDashboardPage,
  page,
}) => {
  await page.addInitScript(() => {
    const getContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type: string, ...rest: unknown[]) {
      return type.startsWith('webgl') ? null : getContext.call(this, type, ...(rest as []));
    } as typeof getContext;
  });
  const dashboardPage = await gotoDashboardPage(dashboard);
  const panel = dashboardPage.getPanelByTitle('Kartverket topo').locator;
  await expect(panel.getByTestId('navimow-map-message')).toContainText('hardware acceleration');
});

test('the map resizes with the panel', async ({ gotoDashboardPage, page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  const dashboardPage = await gotoDashboardPage(dashboard);
  const canvas = dashboardPage.getPanelByTitle('Kartverket topo').locator.locator('canvas.maplibregl-canvas');
  await expect(canvas).toBeVisible();
  const before = (await canvas.boundingBox())!.width;

  await page.setViewportSize({ width: 1000, height: 1000 });
  await expect.poll(async () => (await canvas.boundingBox())!.width).toBeLessThan(before * 0.8);
});

test('each panel releases its map when it unmounts', async ({ gotoDashboardPage, page }) => {
  const warnings: string[] = [];
  page.on('console', (m) => m.text().includes('Too many active WebGL contexts') && warnings.push(m.text()));
  await gotoDashboardPage({ uid: 'navimow-map-lifecycle' });
  const maps = page.locator('canvas.maplibregl-canvas');
  const row = page.getByRole('button', { name: /Map row/ }).first();
  await expect(maps).toHaveCount(1);

  // Collapsing a row unmounts its panels. Twenty remounts pass Chromium's cap of 16 live WebGL
  // contexts if any leak, and it then evicts the oldest with a console warning.
  for (let round = 0; round < 20; round++) {
    await row.click();
    await expect(maps).toHaveCount(0);
    await row.click();
    await expect(maps).toHaveCount(1);
  }
  expect(warnings).toEqual([]);
});
