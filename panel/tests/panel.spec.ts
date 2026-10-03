import path from 'node:path';
import type { Locator, Page } from '@playwright/test';
import { test as base, expect } from '@grafana/plugin-e2e';
import type { Dashboard } from '@grafana/plugin-e2e';

const PLUGIN_ID = 'randax-navimowmap-panel';
const TILE_HOSTS = /^https:\/\/(cache\.kartverket\.no|tile\.openstreetmap\.org|wms\.geonorge\.no)\//;

const test = base.extend<{
  tiles: void;
  pluginErrors: string[];
  mapDashboard: Dashboard;
  openMap: (title: string) => Promise<Locator>;
  openTrail: (title: string) => Promise<Locator>;
}>({
  // Pull requests must not depend on, or load, third-party tile services: tiles come from a fixture.
  // The nightly run sets LIVE_TILES=1 to exercise the real hosts.
  tiles: [
    async ({ page }, use) => {
      if (!process.env.LIVE_TILES) {
        await page.route(TILE_HOSTS, (route) =>
          route.fulfill({
            path: path.join(__dirname, 'fixtures/tile.png'),
            contentType: 'image/png',
            headers: { 'access-control-allow-origin': '*' },
          })
        );
      }
      await use();
    },
    { auto: true },
  ],
  // Console errors, warnings and uncaught exceptions from this plugin (MapLibre included), collected
  // for the whole test and checked when it ends, so late failures are not missed.
  pluginErrors: [
    async ({ page }, use) => {
      const errors: string[] = [];
      page.on('console', (m) => {
        const fromPlugin = m.text().includes('[navimow-map]') || m.location().url.includes(PLUGIN_ID);
        if (['error', 'warning'].includes(m.type()) && fromPlugin) {
          errors.push(m.text());
        }
      });
      page.on('pageerror', (e) => {
        if (e.stack?.includes(PLUGIN_ID)) {
          errors.push(e.message);
        }
      });
      await use(errors);
      expect(errors, 'errors or warnings logged by the plugin').toEqual([]);
    },
    { auto: true },
  ],
  mapDashboard: async ({ readProvisionedDashboard }, use) =>
    use(await readProvisionedDashboard({ fileName: 'navimow-map.json' })),
  openMap: async ({ gotoDashboardPage, mapDashboard }, use) =>
    use(async (title) => (await gotoDashboardPage(mapDashboard)).getPanelByTitle(title).locator),
  // Panels fed by TestData with a slice of the real Trail fixture, fixtures/trail-2026-09-21.csv.
  openTrail: async ({ gotoDashboardPage, readProvisionedDashboard }, use) =>
    use(async (title) => {
      const dashboard = await readProvisionedDashboard({ fileName: 'navimow-trail.json' });
      return (await gotoDashboardPage(dashboard)).getPanelByTitle(title).locator;
    }),
});

/** Waits for an image tile from the host; a 200 carrying an error document does not count. */
const tileFrom = (page: Page, host: string, url: RegExp = /./) =>
  page.waitForResponse(
    (r) =>
      new URL(r.url()).host === host &&
      url.test(r.url()) &&
      r.status() === 200 &&
      /^image\//.test(r.headers()['content-type'] ?? '')
  );

/** The map has loaded and drawn every visible tile. */
const expectDrawn = (panel: Locator) => expect(panel.getByTestId('navimow-map')).toHaveAttribute('data-map-idle');

test('draws the default Kartverket Base map with its attribution, uncollapsed', async ({ openMap, page }) => {
  const [panel] = await Promise.all([openMap('Kartverket topo'), tileFrom(page, 'cache.kartverket.no')]);
  await expectDrawn(panel);
  const attribution = panel.locator('.maplibregl-ctrl-attrib');
  await expect(attribution).toContainText('© Kartverket');
  await expect(attribution).not.toHaveClass(/maplibregl-compact/);
});

test('OpenStreetMap credits its contributors', async ({ openMap, page }) => {
  const [panel] = await Promise.all([openMap('OpenStreetMap'), tileFrom(page, 'tile.openstreetmap.org')]);
  await expectDrawn(panel);
  const attribution = panel.locator('.maplibregl-ctrl-attrib');
  await expect(attribution).toContainText('© OpenStreetMap contributors');
  // Plain-text credit must stay dark on the light attribution strip, whatever Grafana's theme.
  await expect(attribution.locator('.maplibregl-ctrl-attrib-inner')).toHaveCSS('color', 'rgba(0, 0, 0, 0.75)');
});

test('a custom WMS bounding-box template draws tiles from its own host', async ({ openMap, page }) => {
  const [panel] = await Promise.all([
    openMap('Custom WMS'),
    // MapLibre must have filled in the template, not sent it literally.
    tileFrom(page, 'wms.geonorge.no', /BBOX=-?\d/),
  ]);
  await expectDrawn(panel);
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© Kartverket');
});

test('a custom Base map without attribution shows why instead of a map', async ({ openMap }) => {
  const panel = await openMap('Custom without attribution');
  await expect(panel.getByTestId('navimow-map-message')).toContainText('needs an attribution');
  await expect(panel.locator('canvas')).toHaveCount(0);
});

test('the Base map picker offers the presets and switches the map', async ({
  gotoPanelEditPage,
  mapDashboard,
  selectors,
  page,
}) => {
  const panelEditPage = await gotoPanelEditPage({ dashboard: mapDashboard, id: '1' });
  const picker = panelEditPage.getCustomOptions('Base map').getSelect('Base map');
  await picker.locator().getByRole('combobox').click();
  await expect(panelEditPage.getByGrafanaSelector(selectors.components.Select.option)).toHaveText([
    /^Kartverket topo/,
    /^Kartverket topo gråtone/,
    /^Kartverket turkart \(toporaster\)/,
    /^OpenStreetMap.*usage policy/,
    /^Custom/,
  ]);
  await page.keyboard.press('Escape');

  await Promise.all([picker.selectOption('OpenStreetMap'), tileFrom(page, 'tile.openstreetmap.org')]);
  await expectDrawn(panelEditPage.panel.locator);
  await expect(panelEditPage.panel.locator.locator('.maplibregl-ctrl-attrib')).toContainText('OpenStreetMap');
});

test('a browser without WebGL 2 gets a readable message instead of a blank panel', async ({ openMap, page }) => {
  await page.addInitScript(() => {
    const getContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, type: string, ...rest: unknown[]) {
      return type === 'webgl2' ? null : getContext.call(this, type, ...(rest as []));
    } as typeof getContext;
  });
  const panel = await openMap('Kartverket topo');
  await expect(panel.getByTestId('navimow-map-message')).toContainText('hardware acceleration');
});

test('the map resizes with the panel', async ({ openMap, page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  const canvas = (await openMap('Kartverket topo')).locator('canvas.maplibregl-canvas');
  await expect(canvas).toBeVisible();
  const before = (await canvas.boundingBox())!.width;

  await page.setViewportSize({ width: 1000, height: 1000 });
  await expect.poll(async () => (await canvas.boundingBox())!.width).toBeLessThan(before * 0.8);
});

test('a provisioned real Trail is drawn, with the mower faded and its age stated', async ({ openTrail }) => {
  const panel = await openTrail('Real Trail');
  await expect(panel.getByTestId('navimow-map')).toHaveAttribute('data-trails-drawn', '1');
  // The fixture is from September 2026, so its last position is days old by now.
  await expect(panel.getByText(/^Last seen \d+ d ago$/)).toBeVisible();
  await expect(panel.getByRole('img', { name: /^Mower, last seen/ })).toBeVisible();
});

test('a frame without the Trail columns names the missing ones instead of drawing', async ({ openTrail }) => {
  const panel = await openTrail('Unmatched columns');
  await expect(panel.getByTestId('navimow-map-message')).toContainText('No "x" and "y" columns');
});

test('each panel releases its map when it unmounts', async ({
  gotoDashboardPage,
  readProvisionedDashboard,
  selectors,
  page,
}) => {
  const warnings: string[] = [];
  page.on('console', (m) => m.text().includes('Too many active WebGL contexts') && warnings.push(m.text()));
  const dashboardPage = await gotoDashboardPage(
    await readProvisionedDashboard({ fileName: 'navimow-map-lifecycle.json' })
  );
  const maps = page.locator('canvas.maplibregl-canvas');
  const row = dashboardPage.getByGrafanaSelector(selectors.components.DashboardRow.title('Map row'));
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
