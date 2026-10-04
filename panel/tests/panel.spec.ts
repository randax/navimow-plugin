import path from 'node:path';
import type { Locator, Page } from '@playwright/test';
import { test as base, expect } from '@grafana/plugin-e2e';
import type { Dashboard } from '@grafana/plugin-e2e';

const PLUGIN_ID = 'randax-navimowmap-panel';
const LIVE_TILES = Boolean(process.env.LIVE_TILES);
// What each tile host serves in place of its tiles: a Base map of plain green, an Overlay of plain
// blue, and Terrain that is 400 m high everywhere (terrarium: 129 × 256 + 144 − 32768).
const FIXTURE_TILES: Record<string, string> = {
  'cache.kartverket.no': 'tile.png',
  'tile.openstreetmap.org': 'tile.png',
  'wms.geonorge.no': 'overlay.png',
  'tiles.mapterhorn.com': 'terrain-400m.png',
  's3.amazonaws.com': 'terrain-400m.png',
};

const test = base.extend<{
  tiles: void;
  pluginErrors: string[];
  mapDashboard: Dashboard;
  openMap: (title: string) => Promise<Locator>;
  trailDashboard: Dashboard;
  openTrail: (title: string) => Promise<Locator>;
  openTerrain: (title: string) => Promise<Locator>;
}>({
  // Pull requests must not depend on, or load, third-party tile services: tiles come from fixtures.
  // The nightly run sets LIVE_TILES=1 to exercise the real hosts.
  tiles: [
    async ({ page }, use) => {
      if (!LIVE_TILES) {
        await page.route(
          (url) => url.host in FIXTURE_TILES,
          (route) =>
            route.fulfill({
              path: path.join(__dirname, 'fixtures', FIXTURE_TILES[new URL(route.request().url()).host]),
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
  trailDashboard: async ({ readProvisionedDashboard }, use) =>
    use(await readProvisionedDashboard({ fileName: 'navimow-trail.json' })),
  openTrail: async ({ gotoDashboardPage, trailDashboard }, use) =>
    use(async (title) => (await gotoDashboardPage(trailDashboard)).getPanelByTitle(title).locator),
  // The same real Trail, with Terrain or an Overlay.
  openTerrain: async ({ gotoDashboardPage, readProvisionedDashboard }, use) => {
    const dashboard = await readProvisionedDashboard({ fileName: 'navimow-terrain.json' });
    await use(async (title) => (await gotoDashboardPage(dashboard)).getPanelByTitle(title).locator);
  },
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

interface Camera {
  center: [number, number];
  zoom: number;
  bearing: number;
  pitch: number;
  /** Height of the ground the camera looks at: 0 on a flat map. */
  groundElevation: number;
}

/** Where the map was looking from when it last came to rest. */
const cameraOf = async (panel: Locator): Promise<Camera> =>
  JSON.parse((await panel.getByTestId('navimow-map').getAttribute('data-camera')) ?? '{}');

/** Waits for the map to come to rest with the camera as `at` describes. */
const cameraAtRest = async (panel: Locator, at: (camera: Camera) => boolean): Promise<Camera> => {
  await expect.poll(async () => at(await cameraOf(panel))).toBe(true);
  return cameraOf(panel);
};

/** The same place from the same distance and bearing: to a centimetre on the ground. */
const expectSamePlace = (now: Camera, before: Camera) => {
  expect(now.center[0]).toBeCloseTo(before.center[0], 6);
  expect(now.center[1]).toBeCloseTo(before.center[1], 6);
  expect(now.zoom).toBeCloseTo(before.zoom, 3);
  expect(now.bearing).toBeCloseTo(before.bearing, 3);
};

/**
 * Counts the map's pixels as the browser drew them, by what colours them: the fixture Base map
 * green, the fixture Overlay blue, the Trail red.
 */
const pixelsOf = async (page: Page, panel: Locator) => {
  const screenshot = await panel.getByTestId('navimow-map').screenshot();
  return page.evaluate(async (png) => {
    const image = new Image();
    image.src = `data:image/png;base64,${png}`;
    await image.decode();
    const canvas = Object.assign(document.createElement('canvas'), { width: image.width, height: image.height });
    const context = canvas.getContext('2d')!;
    context.drawImage(image, 0, 0);
    const { data } = context.getImageData(0, 0, image.width, image.height);
    const count = { baseMap: 0, overlay: 0, trail: 0 };
    for (let i = 0; i < data.length; i += 4) {
      const [r, g, b] = [data[i], data[i + 1], data[i + 2]];
      if (r > 150 && g < 110) {
        count.trail++;
      } else if (b > 200 && r < 60 && g < 60) {
        count.overlay++;
      } else if (g > 150 && r < 160 && b < 150) {
        count.baseMap++;
      }
    }
    return count;
  }, screenshot.toString('base64'));
};
const NEEDS_FIXTURE_TILES = 'real tiles have no known colours or height';

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

test('the switch on the panel recreates the map in terrain and back, without the view jumping', async ({
  openTerrain,
  page,
}) => {
  const panel = await openTerrain('Starts flat');
  const map = panel.getByTestId('navimow-map');
  const view = panel.getByTestId('navimow-map-view');
  await expectDrawn(panel);
  await expect(view.getByRole('radio', { name: 'Flat' })).toBeChecked();
  const framed = await cameraOf(panel);
  expect(framed).toMatchObject({ pitch: 0, groundElevation: 0 });

  // Zoom in on a corner, to show the view is the owner's and is not framed on the Trail again.
  await map.hover({ position: { x: 100, y: 100 } });
  await page.mouse.wheel(0, -300);
  const before = await cameraAtRest(panel, (camera) => camera.zoom > framed.zoom + 0.2);
  expect(before.center).not.toEqual(framed.center);
  const flatCanvas = await panel.locator('canvas.maplibregl-canvas').elementHandle();

  await Promise.all([view.getByRole('radio', { name: 'Terrain' }).check(), tileFrom(page, 'tiles.mapterhorn.com')]);
  // The ground is far above sea level here: 400 m in the fixture, some 330 m in reality.
  const tilted = await cameraAtRest(panel, (camera) => camera.groundElevation > 100);
  await expectDrawn(panel);
  // A new map, with Terrain in its first style, rather than the flat one with Terrain added.
  expect(await flatCanvas?.evaluate((canvas) => canvas.isConnected)).toBe(false);
  expectSamePlace(tilted, before);
  expect(tilted.pitch).toBe(60);
  await expect(map).toHaveAttribute('data-trails-drawn', '1');
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© Mapterhorn');
  await expect(panel.getByRole('img', { name: /^Mower/ })).toBeVisible();

  await view.getByRole('radio', { name: 'Flat' }).check();
  const flat = await cameraAtRest(panel, (camera) => camera.groundElevation === 0);
  await expectDrawn(panel);
  expect(flat.pitch).toBe(0);
  expectSamePlace(flat, before);
  await expect(map).toHaveAttribute('data-trails-drawn', '1');
  await expect(panel.locator('.maplibregl-ctrl-attrib')).not.toContainText('Mapterhorn');
  await expect(panel.locator('canvas.maplibregl-canvas')).toHaveCount(1);
});

test('a panel can start in terrain, and its camera tilts and turns', async ({ openTerrain, page }) => {
  const panel = await openTerrain('Starts in terrain');
  await expect(panel.getByTestId('navimow-map-view').getByRole('radio', { name: 'Terrain' })).toBeChecked();
  const start = await cameraAtRest(panel, (camera) => camera.groundElevation > 100);
  expect(start).toMatchObject({ pitch: 60, bearing: 0 });
  await expect(panel.getByTestId('navimow-map')).toHaveAttribute('data-trails-drawn', '1');

  // Dragging with the right button turns the camera sideways and tilts it up and down.
  const box = (await panel.getByTestId('navimow-map').boundingBox())!;
  const [x, y] = [box.x + box.width / 2, box.y + box.height / 2];
  await page.mouse.move(x, y);
  await page.mouse.down({ button: 'right' });
  await page.mouse.move(x + 120, y + 40, { steps: 10 });
  await page.mouse.up({ button: 'right' });
  const turned = await cameraAtRest(panel, (camera) => Math.abs(camera.bearing) > 5);
  expect(turned.pitch).toBeLessThan(55);
});

test('a Trail drawn on Terrain follows the ground', async ({ openTerrain, page }) => {
  test.skip(LIVE_TILES, NEEDS_FIXTURE_TILES);
  const panel = await openTerrain('Starts in terrain');
  await cameraAtRest(panel, (camera) => Math.round(camera.groundElevation) === 400);
  await expectDrawn(panel);
  // The camera looks at ground 400 m up. A Trail left at sea level would be far out of this view.
  expect((await pixelsOf(page, panel)).trail).toBeGreaterThan(100);
});

test('the Overlay is drawn over the Base map and under the Trail', async ({ openTerrain, page }) => {
  test.skip(LIVE_TILES, NEEDS_FIXTURE_TILES);
  const [panel] = await Promise.all([openTerrain('Overlay'), tileFrom(page, 'wms.geonorge.no', /BBOX=-?\d/)]);
  await expectDrawn(panel);
  await expect(panel.getByTestId('navimow-map')).toHaveAttribute('data-trails-drawn', '1');
  // At full opacity the Overlay hides the Base map, and the Trail still shows on top of it. Retried:
  // under load, a screenshot has caught a few Base map pixels the Overlay had yet to cover.
  await expect(async () => {
    const pixels = await pixelsOf(page, panel);
    expect(pixels.baseMap).toBe(0);
    expect(pixels.overlay).toBeGreaterThan(10_000);
    expect(pixels.trail).toBeGreaterThan(100);
  }).toPass({ timeout: 20_000 });
  // Without Terrain there is nothing to switch between.
  await expect(panel.getByTestId('navimow-map-view')).toHaveCount(0);
});

test('Terrain is off until enabled, then defaults to Mapterhorn with any Base map', async ({
  gotoPanelEditPage,
  mapDashboard,
  selectors,
  page,
}) => {
  const panelEditPage = await gotoPanelEditPage({ dashboard: mapDashboard, id: '2' });
  const panel = panelEditPage.panel.locator;
  const options = panelEditPage.getCustomOptions('Terrain');
  await expect(options.getSwitch('Enable')).not.toBeChecked();
  await expect(panel.getByTestId('navimow-map-view')).toHaveCount(0);

  await Promise.all([options.getSwitch('Enable').check(), tileFrom(page, 'tiles.mapterhorn.com')]);
  await expect(options.getSelect('Source')).toHaveSelected('Mapterhorn');
  await expect(options.getRadioGroup('Start in')).toHaveChecked('Terrain');
  await expect(panel.getByTestId('navimow-map-view').getByRole('radio', { name: 'Terrain' })).toBeChecked();
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© Mapterhorn');
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© OpenStreetMap contributors');

  await options.getSelect('Source').locator().getByRole('combobox').click();
  await expect(panelEditPage.getByGrafanaSelector(selectors.components.Select.option)).toHaveText([
    /^Mapterhorn.*1 m detail/,
    /^AWS Terrain Tiles.*10 m detail/,
    /^Custom/,
  ]);
  await page.keyboard.press('Escape');
  await Promise.all([
    options.getSelect('Source').selectOption('AWS Terrain Tiles'),
    tileFrom(page, 's3.amazonaws.com', /elevation-tiles-prod\/terrarium/),
  ]);
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('Norway terrain data © Kartverket');

  // The custom slot asks for what it needs, in place of the map, until it has it.
  await options.getSelect('Source').selectOption('Custom');
  await expect(panel.getByTestId('navimow-map-message')).toContainText('A custom Terrain needs an http(s) URL');
  await expect(options.getRadioGroup('Encoding')).toHaveChecked('Terrarium');
  await expect(options.getNumberInput('Tile size')).toHaveValue('512');
  await expect(options.getNumberInput('Max zoom')).toHaveValue('16');
  await options.getTextInput('Attribution').fill('© My elevation tiles');
  await options.getTextInput('URL template').fill('https://tiles.mapterhorn.com/{z}/{x}/{y}.webp');
  await page.keyboard.press('Tab');
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© My elevation tiles');

  // Only a change of Terrain recreates the map: another Base map restyles the one that is there.
  const canvas = await panel.locator('canvas.maplibregl-canvas').elementHandle();
  await Promise.all([
    panelEditPage.getCustomOptions('Base map').getSelect('Base map').selectOption('Kartverket topo'),
    tileFrom(page, 'cache.kartverket.no'),
  ]);
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© Kartverket');
  expect(await canvas?.evaluate((element) => element.isConnected)).toBe(true);
});

test('the Overlay picker offers hillshade or a custom URL, with an opacity', async ({
  gotoPanelEditPage,
  mapDashboard,
  selectors,
  page,
}) => {
  const panelEditPage = await gotoPanelEditPage({ dashboard: mapDashboard, id: '2' });
  const panel = panelEditPage.panel.locator;
  const options = panelEditPage.getCustomOptions('Overlay');
  await expect(options.getSelect('Overlay')).toHaveSelected('None');

  await options.getSelect('Overlay').locator().getByRole('combobox').click();
  await expect(panelEditPage.getByGrafanaSelector(selectors.components.Select.option)).toHaveText([
    /^None/,
    /^Kartverket hillshade/,
    /^Custom.*licence/,
  ]);
  await page.keyboard.press('Escape');
  await Promise.all([
    options.getSelect('Overlay').selectOption('Kartverket hillshade'),
    tileFrom(page, 'wms.geonorge.no', /LAYERS=DTM:skyggerelieff.*BBOX=-?\d/),
  ]);
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© Kartverket');
  await expect(options.getSliderInput('Opacity')).toHaveValue('0.5');

  await options.getSelect('Overlay').selectOption('Custom');
  await expect(panel.getByTestId('navimow-map-message')).toContainText('A custom Overlay needs an http(s) URL');
  await options.getTextInput('Attribution').fill('© My imagery');
  await options.getTextInput('URL template').fill('https://tile.openstreetmap.org/{z}/{x}/{y}.png?token=mine');
  await Promise.all([page.keyboard.press('Tab'), tileFrom(page, 'tile.openstreetmap.org', /token=mine/)]);
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© My imagery');
});
