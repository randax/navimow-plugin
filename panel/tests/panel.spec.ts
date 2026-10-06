import { readFileSync } from 'node:fs';
import path from 'node:path';
import type { Locator, Page } from '@playwright/test';
import { test as base, expect } from '@grafana/plugin-e2e';
import type { Dashboard, DashboardPage } from '@grafana/plugin-e2e';
import type { Camera as MapCamera } from '../src/model/view';

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
  terrainDashboard: Dashboard;
  openTerrain: (title: string) => Promise<Locator>;
  overlayDashboard: Dashboard;
  interactionDashboard: Dashboard;
  openInteraction: (title: string) => Promise<Locator>;
  coverageDashboard: Dashboard;
  openCoverage: (title: string) => Promise<Locator>;
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
  // The same real Trail, with Terrain.
  terrainDashboard: async ({ readProvisionedDashboard }, use) =>
    use(await readProvisionedDashboard({ fileName: 'navimow-terrain.json' })),
  openTerrain: async ({ gotoDashboardPage, terrainDashboard }, use) =>
    use(async (title) => (await gotoDashboardPage(terrainDashboard)).getPanelByTitle(title).locator),
  // And with the hillshade Overlay, on a dashboard of its own: the real hillshade service is slow,
  // and loaded beside the Terrain panels it slowed their tests towards the timeout.
  overlayDashboard: async ({ readProvisionedDashboard }, use) =>
    use(await readProvisionedDashboard({ fileName: 'navimow-overlay.json' })),
  // A made-up lawn, laid out so that a test knows where things are: the dock in the middle of the
  // map, Job a running north through it in Zone 1, and Job b in a U around that, 15 m to each side.
  interactionDashboard: async ({ readProvisionedDashboard }, use) =>
    use(await readProvisionedDashboard({ fileName: 'navimow-interaction.json' })),
  openInteraction: async ({ gotoDashboardPage, interactionDashboard }, use) =>
    use(async (title) => (await gotoDashboardPage(interactionDashboard)).getPanelByTitle(title).locator),
  // The real Trail again, with Coverage in each style, and raised over Terrain.
  coverageDashboard: async ({ readProvisionedDashboard }, use) =>
    use(await readProvisionedDashboard({ fileName: 'navimow-coverage.json' })),
  openCoverage: async ({ gotoDashboardPage, coverageDashboard }, use) =>
    use(async (title) => (await gotoDashboardPage(coverageDashboard)).getPanelByTitle(title).locator),
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

/** The map's camera, and the height of the ground it looks at: 0 on a flat map. */
type Camera = MapCamera & { groundElevation: number };

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
    // The colour near the bottom left corner as well, clear of the Trail, the mower, the controls
    // and the attribution.
    const corner = 4 * ((image.height - 30) * image.width + 30);
    const count = { baseMap: 0, overlay: 0, trail: 0, corner: [...data.slice(corner, corner + 3)] };
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

// The interaction dashboard's dock, and the length of a degree there.
const DOCK = { lon: 10.672, lat: 59.964 };
const METRES_PER_DEGREE = { lon: 55860.6, lat: 111411.7 };

/**
 * A place on the interaction dashboard's map, in metres east and north of the dock, as a position
 * to point at. The map there looks straight down with north up, where a metre is the same number
 * of pixels in both directions.
 */
const metresFromDock = async (panel: Locator, east: number, north: number) => {
  const { center, zoom } = await cameraOf(panel);
  const pixelsPerMetre = (512 * 2 ** zoom) / (40_075_016.686 * Math.cos((center[1] * Math.PI) / 180));
  const box = (await panel.getByTestId('navimow-map').boundingBox())!;
  return {
    x: box.width / 2 + (east - (center[0] - DOCK.lon) * METRES_PER_DEGREE.lon) * pixelsPerMetre,
    y: box.height / 2 - (north - (center[1] - DOCK.lat) * METRES_PER_DEGREE.lat) * pixelsPerMetre,
  };
};

/** Moves the pointer onto a place on the map, as a hand would: arriving, not appearing. */
const pointAt = async (panel: Locator, east: number, north: number) => {
  const map = panel.getByTestId('navimow-map');
  const { x, y } = await metresFromDock(panel, east, north);
  await map.hover({ position: { x: x + 2, y: y + 2 } });
  await map.hover({ position: { x, y } });
};

/** The map's control with this name. */
const control = (panel: Locator, name: string) =>
  panel.getByTestId('navimow-map-controls').getByRole('button', { name });

/** Shows or hides something from the panel's list, puts the list away and waits for the map to come to rest. */
const setShown = async (page: Page, panel: Locator, name: string, shown: boolean) => {
  await control(panel, 'Show or hide').click();
  // Sought on the page: the list opens outside the panel, as every Grafana popover does.
  const entry = page.getByRole('checkbox', { name });
  await (shown ? entry.check({ force: true }) : entry.uncheck({ force: true }));
  await page.keyboard.press('Escape');
  await expect(entry).toHaveCount(0);
  await expectDrawn(panel);
};

/** Sets how Coverage is drawn from its control on the panel, puts the control away and waits for the map. */
const drawCoverage = async (page: Page, panel: Locator, style: string, raised: boolean) => {
  await control(panel, 'Coverage').click();
  // Sought on the page: it opens outside the panel, as every Grafana popover does.
  const coverage = page.getByTestId('navimow-map-coverage');
  await coverage.getByRole('radio', { name: style }).check({ force: true });
  await coverage.getByRole('checkbox', { name: 'Raised' }).setChecked(raised, { force: true });
  await page.keyboard.press('Escape');
  await expect(coverage).toHaveCount(0);
  await expectDrawn(panel);
};

/** How much of the fixture Base map's green shows, in pixels: less of it wherever Coverage is drawn. */
const baseMapPixels = async (page: Page, panel: Locator) => (await pixelsOf(page, panel)).baseMap;

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

test('positions read from the wrong column are named, instead of the map failing on them', async ({ openTrail }) => {
  // X is set to the time column, so epoch milliseconds are read as metres: a latitude no map can hold.
  const panel = await openTrail('Time as x');
  await expect(panel.getByTestId('navimow-map-message')).toContainText(
    'The "time" and "postureY" columns put every position more than 10 km from the dock'
  );
});

// Grafana migrates the options of a panel saved under another plugin version, and of no other. This
// panel is saved under the current one, with its Lawn where earlier versions kept it, so nothing has
// moved it: the panel and its editors have to read it from there themselves.
test('a panel saved by an earlier version keeps its whole Dock origin when one value of it is edited', async ({
  gotoPanelEditPage,
  trailDashboard,
  page,
}) => {
  // Under any other version Grafana would migrate the panel as it loads, and this would prove nothing.
  const saved: { panels: Array<{ id: number; pluginVersion?: string }> } = JSON.parse(
    readFileSync(path.join(__dirname, '../provisioning/dashboards/navimow-trail.json'), 'utf8')
  );
  const { version } = JSON.parse(readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
  expect(saved.panels.find((p) => p.id === 4)?.pluginVersion, 'saved under the version the plugin is built as').toBe(
    version
  );

  const current = await gotoPanelEditPage({ dashboard: trailDashboard, id: '1' });
  await expectDrawn(current.panel.locator);
  const placed = await cameraOf(current.panel.locator);

  const panelEditPage = await gotoPanelEditPage({ dashboard: trailDashboard, id: '4' });
  const panel = panelEditPage.panel.locator;
  const options = panelEditPage.getCustomOptions('Dock origin and Boundary');
  await expectDrawn(panel);
  // The same Trail, placed and turned exactly as on the panel saved in today's shape: turned any
  // other way about the dock, its middle would be somewhere else.
  const legacy = await cameraOf(panel);
  expect(legacy.center[0]).toBeCloseTo(placed.center[0], 6);
  expect(legacy.center[1]).toBeCloseTo(placed.center[1], 6);
  await expect(options.getNumberInput('Latitude')).toHaveValue('59.964');
  await expect(options.getNumberInput('Longitude')).toHaveValue('10.672');
  await expect(options.getNumberInput('Rotation')).toHaveValue('20');

  await options.getNumberInput('Latitude').fill('59.965');
  await page.keyboard.press('Tab');
  await cameraAtRest(panel, (camera) => camera.center[1] > placed.center[1] + 0.0005);
  await expect(panel.getByTestId('navimow-map')).toHaveAttribute('data-trails-drawn', '1');
  await expect(options.getNumberInput('Longitude')).toHaveValue('10.672');
  await expect(options.getNumberInput('Rotation')).toHaveValue('20');

  // Half a number left behind, here the minus of a longitude never finished, is not a value taken out.
  await options.getNumberInput('Longitude').press('ControlOrMeta+a');
  await page.keyboard.type('-');
  await page.keyboard.press('Tab');
  await expect(options.getNumberInput('Longitude')).toHaveValue('10.672');

  // A value taken out stays out, though the place the panel was saved in still holds it.
  await options.getNumberInput('Rotation').fill('');
  await page.keyboard.press('Tab');
  await expect(options.getNumberInput('Latitude')).toHaveValue('59.965');
  await expect(options.getNumberInput('Rotation')).toHaveValue('');
  await expectDrawn(panel);
  await expect(panel.getByTestId('navimow-map')).toHaveAttribute('data-trails-drawn', '1');
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
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© Mapterhorn, © Kartverket');
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

// Not proof that the Trail bends over relief: the fixture ground is level, and MapLibre has no way to
// draw a line layer off the ground that a test could tell apart. That the Trail is a line layer, laid
// over the Terrain, is the style's claim (style.test.ts); this shows it on screen once the ground is up.
test('the Trail is drawn in the terrain view, on ground 400 m up', async ({ openTerrain, page }) => {
  test.skip(LIVE_TILES, NEEDS_FIXTURE_TILES);
  const panel = await openTerrain('Starts in terrain');
  await cameraAtRest(panel, (camera) => Math.round(camera.groundElevation) === 400);
  await expectDrawn(panel);
  expect((await pixelsOf(page, panel)).trail).toBeGreaterThan(100);
});

test('the Overlay is drawn over the Base map and under the Trail', async ({
  gotoDashboardPage,
  overlayDashboard,
  page,
}) => {
  test.skip(LIVE_TILES, NEEDS_FIXTURE_TILES);
  const [dashboardPage] = await Promise.all([
    gotoDashboardPage(overlayDashboard),
    tileFrom(page, 'wms.geonorge.no', /BBOX=-?\d/),
  ]);
  const panel = dashboardPage.getPanelByTitle('Overlay').locator;
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

test('a Base map switch keeps the Overlay and the Trail, and the Overlay fades with its opacity', async ({
  gotoPanelEditPage,
  overlayDashboard,
  page,
}) => {
  test.skip(LIVE_TILES, NEEDS_FIXTURE_TILES);
  const panelEditPage = await gotoPanelEditPage({ dashboard: overlayDashboard, id: '1' });
  const panel = panelEditPage.panel.locator;
  await expectDrawn(panel);
  const canvas = await panel.locator('canvas.maplibregl-canvas').elementHandle();

  await Promise.all([
    panelEditPage.getCustomOptions('Base map').getSelect('Base map').selectOption('OpenStreetMap'),
    tileFrom(page, 'tile.openstreetmap.org'),
  ]);
  // The new style is in, on the same map, and has been drawn.
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© OpenStreetMap contributors');
  await expectDrawn(panel);
  expect(await canvas?.evaluate((element) => element.isConnected)).toBe(true);
  // The Overlay, at full opacity, still hides the Base map under it, and the Trail still shows on top.
  await expect(async () => {
    const pixels = await pixelsOf(page, panel);
    expect(pixels.baseMap).toBe(0);
    expect(pixels.overlay).toBeGreaterThan(10_000);
    expect(pixels.trail).toBeGreaterThan(100);
  }).toPass({ timeout: 20_000 });

  // At a quarter opacity it tints the Base map: three parts fixture green (122, 184, 107) to one
  // part fixture blue (0, 0, 255).
  await panelEditPage.getCustomOptions('Overlay').getSliderInput('Opacity').fill('0.25');
  await page.keyboard.press('Tab');
  await expect(async () => {
    const [red, green, blue] = (await pixelsOf(page, panel)).corner;
    expect(Math.abs(red - 92)).toBeLessThan(10);
    expect(Math.abs(green - 138)).toBeLessThan(10);
    expect(Math.abs(blue - 144)).toBeLessThan(10);
  }).toPass({ timeout: 20_000 });
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
  // Kartverket's elevation data is on screen, so it is credited even where the Base map is not its own.
  await expect(panel.locator('.maplibregl-ctrl-attrib')).toContainText('© Mapterhorn, © Kartverket');
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

test('the calibration drawer saves the Dock origin and Boundary into the options, or discards them, and takes its map with it', async ({
  gotoPanelEditPage,
  trailDashboard,
  page,
}) => {
  const panelEditPage = await gotoPanelEditPage({ dashboard: trailDashboard, id: '1' });
  const panel = panelEditPage.panel.locator;
  await expectDrawn(panel);
  const options = panelEditPage.getCustomOptions('Dock origin and Boundary');
  const canvases = page.locator('canvas.maplibregl-canvas');
  const open = async () => {
    await options.element.getByRole('button', { name: 'Calibrate on the map' }).click();
    const drawer = page.getByRole('dialog', { name: 'Dock origin and Boundary' });
    await expect(drawer).toBeVisible();
    return drawer;
  };

  const drawer = await open();
  // The drawer has a map of its own, beside the panel's.
  await expect(canvases).toHaveCount(2);
  const map = drawer.getByTestId('navimow-calibration-map');
  await expect(map).toHaveAttribute('data-map-idle');
  const latitude = drawer.getByRole('spinbutton', { name: 'Latitude' });
  const rotation = drawer.getByRole('spinbutton', { name: 'Rotation in degrees' });
  await expect(latitude).toHaveValue('59.964');
  await expect(rotation).toHaveValue('20');

  // Dragging the handle turns the x-axis; dragging the dock moves it.
  const drag = async (name: string, dx: number, dy: number) => {
    const handle = drawer.getByRole('img', { name });
    // Hovering waits for the marker to stand still: the drawer slides in, the map with it.
    await handle.hover();
    const box = (await handle.boundingBox())!;
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + dx, box.y + box.height / 2 + dy, { steps: 8 });
    await page.mouse.up();
  };
  await drag('Rotation handle', 60, 60);
  await expect(rotation).not.toHaveValue('20');
  const turned = Number(await rotation.inputValue());
  expect(turned).toBeGreaterThan(20);
  expect(turned).toBeLessThan(180);
  await drag('Dock', 0, -40);
  await expect(latitude).not.toHaveValue('59.964');
  expect(Number(await latitude.inputValue())).toBeGreaterThan(59.964);

  await latitude.fill('59.965');
  await rotation.fill('33.5');

  // Four corners around the middle of the map: three clicks and a double click, which must not
  // add its corner twice.
  await drawer.getByRole('button', { name: 'Draw outline' }).click();
  const box = (await map.boundingBox())!;
  const [cx, cy] = [box.width / 2, box.height / 2];
  await map.click({ position: { x: cx - 120, y: cy - 120 } });
  await map.click({ position: { x: cx + 120, y: cy - 120 } });
  await map.click({ position: { x: cx + 120, y: cy + 120 } });
  await map.dblclick({ position: { x: cx - 120, y: cy + 120 } });
  await expect(drawer.getByText('Outline: 4 corners')).toBeVisible();

  // A Zone gets the next identifier; blanking it holds Save until it is back.
  await drawer.getByRole('button', { name: 'Add Zone' }).click();
  await map.click({ position: { x: cx - 60, y: cy - 60 } });
  await map.click({ position: { x: cx + 60, y: cy - 60 } });
  await map.dblclick({ position: { x: cx, y: cy + 60 } });
  const zoneId = drawer.getByRole('textbox', { name: 'Zone 1 identifier' });
  await expect(zoneId).toHaveValue('1');
  const save = drawer.getByRole('button', { name: 'Save' });
  await zoneId.fill('');
  await expect(drawer.getByText(/Zone 1 needs the identifier/)).toBeVisible();
  await expect(save).toBeDisabled();
  await zoneId.fill('3');
  await expect(save).toBeEnabled();

  // What will be saved is shown as text, with the typed values in it.
  const text = drawer.locator('textarea');
  await expect(text).toHaveValue(/"lat": 59\.965,/);
  await expect(text).toHaveValue(/"rotation": 33\.5/);
  await expect(text).toHaveValue(/"outline": \[/);
  await expect(text).toHaveValue(/"id": "3"/);

  await save.click();
  await expect(drawer).toHaveCount(0);
  await expect(canvases).toHaveCount(1);
  await expect(options.getNumberInput('Latitude')).toHaveValue('59.965');
  await expect(options.getNumberInput('Rotation')).toHaveValue('33.5');
  await expectDrawn(panel);
  await expect(panel.getByTestId('navimow-map')).toHaveAttribute('data-trails-drawn', '1');

  // Discard forgets a change, and the plain fields keep what was saved.
  const again = await open();
  await again.getByRole('spinbutton', { name: 'Latitude' }).fill('58');
  await again.getByRole('button', { name: 'Discard' }).click();
  await expect(again).toHaveCount(0);
  await expect(canvases).toHaveCount(1);
  await expect(options.getNumberInput('Latitude')).toHaveValue('59.965');
});

test('hovering a Trail tells its time, Job and Zone, and hovering a Zone its name and latest progress', async ({
  openInteraction,
  page,
}) => {
  const panel = await openInteraction('Two Jobs');
  await expectDrawn(panel);
  await expect(panel.getByTestId('navimow-map')).toHaveAttribute('data-trails-drawn', '2');
  // Sought on the page: Grafana draws tooltips outside the panel, so that the panel cannot clip them.
  const tooltip = page.getByTestId('navimow-map-tooltip');
  await expect(tooltip).toHaveCount(0);

  // Job a passes through the dock half a minute after it began, in the Zone traced as Front lawn.
  await pointAt(panel, 0, 0);
  await expect(tooltip).toContainText(/^\d{4}-\d\d-\d\d \d\d:20:30/);
  await expect(tooltip).toContainText(/Job\s*job-a/);
  await expect(tooltip).toContainText(/Zone\s*Front lawn \(1\)/);
  await expect(tooltip).toContainText(/Status\s*isRunning/);

  // 15 m west of it runs Job b, in a Zone traced without a name.
  await pointAt(panel, -15, 0);
  await expect(tooltip).toContainText(/Job\s*job-b/);
  await expect(tooltip).toContainText(/Zone\s*2/);

  // Between the two, off any Trail, is the grass of Front lawn itself, last reported 64 % mown.
  await pointAt(panel, -4, 10);
  await expect(tooltip).toContainText('Front lawn (1)');
  await expect(tooltip).toContainText(/Progress\s*64%/);
  await expect(tooltip).not.toContainText('Job');

  // Outside the Boundary there is nothing to tell.
  await pointAt(panel, 30, 30);
  await expect(tooltip).toHaveCount(0);
});

test('clicking a Trail sets the Job variable, which narrows the map to that Job until it is All again', async ({
  gotoDashboardPage,
  interactionDashboard,
  page,
}) => {
  const dashboardPage = await gotoDashboardPage(interactionDashboard);
  const panel = dashboardPage.getPanelByTitle('Two Jobs').locator;
  const map = panel.getByTestId('navimow-map');
  await expectDrawn(panel);
  await expect(map).toHaveAttribute('data-trails-drawn', '2');
  const framed = await cameraOf(panel);

  await map.click({ position: await metresFromDock(panel, -15, 0) });
  await expect(page).toHaveURL(/var-job=job-b/);
  await expect(map).toHaveAttribute('data-trails-drawn', '1');
  // Job b is the one that stayed.
  const tooltip = page.getByTestId('navimow-map-tooltip');
  await pointAt(panel, -15, 0);
  await expect(tooltip).toContainText(/Job\s*job-b/);
  // Where Job a ran there is now only the grass of its Zone, and the view has not moved to show it gone.
  await pointAt(panel, 0, 10);
  await expect(tooltip).toContainText(/Progress\s*64%/);
  await expect(tooltip).not.toContainText('Job');
  expectSamePlace(await cameraOf(panel), framed);

  // The dashboard's own picker undoes it.
  await dashboardPage.goto({ queryParams: new URLSearchParams({ 'var-job': '$__all' }) });
  await expectDrawn(panel);
  await expect(map).toHaveAttribute('data-trails-drawn', '2');
});

test('the Job variable is picked from the dashboard’s own variables', async ({
  gotoPanelEditPage,
  interactionDashboard,
  selectors,
  page,
}) => {
  const panelEditPage = await gotoPanelEditPage({ dashboard: interactionDashboard, id: '1' });
  const picker = panelEditPage.getCustomOptions('Jobs').getSelect('Job variable');
  await expect(picker).toHaveSelected('$job');
  await picker.locator().getByRole('combobox').click();
  // The dashboard has two variables, one for the Job and one for the mower.
  await expect(panelEditPage.getByGrafanaSelector(selectors.components.Select.option)).toHaveText(['$job', '$mower']);
  await page.keyboard.press('Escape');
});

test('the controls zoom, turn north up and fit the view to the Trail again', async ({ openInteraction, page }) => {
  const panel = await openInteraction('Two Jobs');
  await expectDrawn(panel);
  const framed = await cameraOf(panel);

  await control(panel, 'Zoom in').click();
  await cameraAtRest(panel, (camera) => Math.abs(camera.zoom - (framed.zoom + 1)) < 0.01);
  await control(panel, 'Zoom out').click();
  await cameraAtRest(panel, (camera) => Math.abs(camera.zoom - framed.zoom) < 0.01);
  await control(panel, 'Zoom out').click();
  await cameraAtRest(panel, (camera) => Math.abs(camera.zoom - (framed.zoom - 1)) < 0.01);

  // Dragging with the right button turns the map; the compass turns it back. Dragged to the left,
  // away from the controls: in a narrow panel a drag to the right ends on one of them.
  const box = (await panel.getByTestId('navimow-map').boundingBox())!;
  const [x, y] = [box.x + box.width / 2, box.y + box.height / 2];
  await page.mouse.move(x, y);
  await page.mouse.down({ button: 'right' });
  await page.mouse.move(x - 120, y, { steps: 10 });
  await page.mouse.up({ button: 'right' });
  await cameraAtRest(panel, (camera) => Math.abs(camera.bearing) > 5);
  await control(panel, 'Turn north up').click();
  await cameraAtRest(panel, (camera) => camera.bearing === 0);

  // Dragged off to one side and still zoomed out, the view is the owner's until they ask for the Trail.
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 60, y + 100, { steps: 10 });
  await page.mouse.up();
  const away = await cameraAtRest(panel, (camera) => Math.abs(camera.center[1] - framed.center[1]) > 0.0001);
  expect(away.zoom).not.toBeCloseTo(framed.zoom, 1);
  await control(panel, 'Fit to Trail').click();
  await cameraAtRest(panel, (camera) => Math.abs(camera.zoom - framed.zoom) < 0.001);
  expectSamePlace(await cameraOf(panel), framed);
});

test('the view is the owner’s until following is switched on, which puts the mower in the middle', async ({
  gotoDashboardPage,
  interactionDashboard,
  page,
}) => {
  const dashboardPage = await gotoDashboardPage(interactionDashboard);
  const panel = dashboardPage.getPanelByTitle('Two Jobs').locator;
  await expectDrawn(panel);
  const follow = control(panel, 'Follow the mower');
  await expect(follow).toHaveAttribute('aria-pressed', 'false');
  // Framed on the whole Trail, which is centred on the dock.
  const framed = await cameraOf(panel);
  expect(framed.center[0]).toBeCloseTo(DOCK.lon, 5);
  expect(framed.center[1]).toBeCloseTo(DOCK.lat, 5);

  // The mower was last at the end of Job b: 20 m south of the dock and 15 m east.
  await follow.click();
  await expect(follow).toHaveAttribute('aria-pressed', 'true');
  const following = await cameraAtRest(panel, (camera) => camera.center[1] < DOCK.lat - 0.0001);
  expect(following.center[0]).toBeCloseTo(DOCK.lon + 15 / METRES_PER_DEGREE.lon, 5);
  expect(following.center[1]).toBeCloseTo(DOCK.lat - 20 / METRES_PER_DEGREE.lat, 5);
  // The zoom stays the owner's.
  expect(following.zoom).toBeCloseTo(framed.zoom, 3);

  // A look around lasts until the next refresh, which puts the mower back in the middle though it has not moved.
  const map = panel.getByTestId('navimow-map');
  const box = (await map.boundingBox())!;
  await map.hover({ position: { x: box.width / 2, y: box.height / 2 } });
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2 + 100, { steps: 10 });
  await page.mouse.up();
  await cameraAtRest(panel, (camera) => camera.center[1] > following.center[1] + 0.0001);
  await dashboardPage.refreshDashboard();
  expectSamePlace(
    await cameraAtRest(panel, (camera) => Math.abs(camera.center[1] - following.center[1]) < 1e-6),
    following
  );
});

test('a panel set to follow the mower starts with it in the middle', async ({ openInteraction }) => {
  const panel = await openInteraction('Follows the mower');
  await expectDrawn(panel);
  await expect(control(panel, 'Follow the mower')).toHaveAttribute('aria-pressed', 'true');
  // Job a ended 15 m north of the dock.
  const camera = await cameraAtRest(panel, (at) => at.center[1] > DOCK.lat + 0.0001);
  expect(camera.center[0]).toBeCloseTo(DOCK.lon, 5);
  expect(camera.center[1]).toBeCloseTo(DOCK.lat + 15 / METRES_PER_DEGREE.lat, 5);
});

test('the Trail and the Boundary can be hidden from the panel, and shown again', async ({ openInteraction, page }) => {
  const panel = await openInteraction('Two Jobs');
  const map = panel.getByTestId('navimow-map');
  await expectDrawn(panel);
  await expect(map).toHaveAttribute('data-trails-drawn', '2');

  const layers = control(panel, 'Show or hide');
  // Sought on the page: the list opens outside the panel, as every Grafana popover does.
  const trail = page.getByRole('checkbox', { name: 'Trail' });
  const boundary = page.getByRole('checkbox', { name: 'Boundary' });
  await layers.click();
  await expect(trail).toBeChecked();
  await expect(boundary).toBeChecked();

  await trail.uncheck({ force: true });
  await expect(map).toHaveAttribute('data-trails-drawn', '0');
  // The mower is not the Trail: it stays.
  await expect(panel.locator('[aria-label^="Mower"]')).toBeVisible();
  await trail.check({ force: true });
  await expect(map).toHaveAttribute('data-trails-drawn', '2');

  // A hidden Boundary has no Zones to hover either.
  await boundary.uncheck({ force: true });
  // The list is put away before going back to the map, as a click on the map would do.
  await page.keyboard.press('Escape');
  await expect(trail).toHaveCount(0);
  await expectDrawn(panel);
  await pointAt(panel, -4, 10);
  await expect(page.getByTestId('navimow-map-tooltip')).toHaveCount(0);

  await layers.click();
  await expect(boundary).not.toBeChecked();
  await boundary.check({ force: true });
  await page.keyboard.press('Escape');
  await expect(trail).toHaveCount(0);
  await expectDrawn(panel);
  await pointAt(panel, -4, 12);
  await expect(page.getByTestId('navimow-map-tooltip')).toContainText('Front lawn (1)');
});

test('positions from two mowers are drawn with a warning that names them', async ({ openInteraction }) => {
  const panel = await openInteraction('Two mowers');
  await expectDrawn(panel);
  await expect(panel.getByTestId('navimow-map')).toHaveAttribute('data-trails-drawn', '2');
  await expect(panel.getByTestId('navimow-map-warning')).toContainText('Positions from 2 mowers, "north" and "south"');
  // A panel for one mower has nothing to warn about.
  const one = await openInteraction('Two Jobs');
  await expectDrawn(one);
  await expect(one.getByTestId('navimow-map-warning')).toHaveCount(0);
});

for (const { title, note } of [
  { title: 'Grid' },
  { title: 'Heatmap, by time since mowed', note: 'A Heatmap shows how often each part was cut, not how long ago.' },
  { title: 'Buffered line' },
]) {
  test(`Coverage of a real Trail is drawn, hidden and shown again: ${title}`, async ({ openCoverage, page }) => {
    test.skip(LIVE_TILES, NEEDS_FIXTURE_TILES);
    const panel = await openCoverage(title);
    await expectDrawn(panel);
    // What a style cannot show of what the options ask for is said beside the map, while it is shown.
    const warning = panel.getByTestId('navimow-map-warning');
    const expectNote = () => (note ? expect(warning).toContainText(note) : expect(warning).toHaveCount(0));
    await expectNote();

    // Without the Trail, whose line lies over the same ground, what hides the Base map is Coverage.
    await setShown(page, panel, 'Trail', false);
    const covered = await baseMapPixels(page, panel);
    await setShown(page, panel, 'Coverage', false);
    expect((await baseMapPixels(page, panel)) - covered).toBeGreaterThan(500);
    await expect(warning).toHaveCount(0);

    await setShown(page, panel, 'Coverage', true);
    await expect(async () => {
      expect(Math.abs((await baseMapPixels(page, panel)) - covered)).toBeLessThan(50);
    }).toPass({ timeout: 20_000 });
    await expectNote();
  });
}

test('the Coverage control switches the style and raises it, tilting the map to show it', async ({
  openCoverage,
  page,
}) => {
  const panel = await openCoverage('Grid');
  await expectDrawn(panel);
  expect((await cameraOf(panel)).pitch).toBe(0);

  await control(panel, 'Coverage').click();
  // Sought on the page: it opens outside the panel, as every Grafana popover does.
  const coverage = page.getByTestId('navimow-map-coverage');
  await expect(coverage.getByRole('radio', { name: 'Grid' })).toBeChecked();
  // The key to the colours is kept with the control.
  await expect(coverage).toContainText('Visits');

  await coverage.getByRole('radio', { name: 'Buffered line' }).check({ force: true });
  await expectDrawn(panel);
  const warning = panel.getByTestId('navimow-map-warning');
  await expect(warning).toHaveCount(0);

  await coverage.getByRole('checkbox', { name: 'Raised' }).check({ force: true });
  await cameraAtRest(panel, (camera) => camera.pitch === 60);
  await expect(warning).toContainText('Raised, a Buffered line shows where the mower has cut, not how often.');

  await coverage.getByRole('radio', { name: 'Heatmap' }).check({ force: true });
  await expect(warning).toHaveCount(0);
  await expectDrawn(panel);
  // Lowering it leaves the tilt, which is the owner's from here.
  await coverage.getByRole('checkbox', { name: 'Raised' }).uncheck({ force: true });
  await expectDrawn(panel);
  expect((await cameraOf(panel)).pitch).toBe(60);
});

test('every style of Coverage is drawn on Terrain 400 m up, raised as the panel opens and flat', async ({
  openCoverage,
  page,
}) => {
  test.skip(LIVE_TILES, NEEDS_FIXTURE_TILES);
  // Twelve redraws over Terrain, under software rendering.
  test.setTimeout(180_000);
  const panel = await openCoverage('Raised over Terrain');
  await cameraAtRest(panel, (camera) => Math.round(camera.groundElevation) === 400 && camera.pitch === 60);
  await expectDrawn(panel);

  await setShown(page, panel, 'Trail', false);
  await setShown(page, panel, 'Coverage', false);
  const bare = await baseMapPixels(page, panel);
  await setShown(page, panel, 'Coverage', true);
  for (const raised of [true, false]) {
    for (const style of ['Grid', 'Heatmap', 'Buffered line']) {
      await drawCoverage(page, panel, style, raised);
      const covered = bare - (await baseMapPixels(page, panel));
      expect(covered, `${style}, ${raised ? 'raised' : 'flat'}`).toBeGreaterThan(500);
    }
  }
});

// The dashboard an owner imports, reading a PostgreSQL which docker compose fills as a collector
// would (tests/seed.py): the real capture's Job twice over, one that ended some hours ago and one a
// week before it with an error and a gap in collection put into it. The Jobs are placed by when the
// database was filled: after a day, fill it again with `docker compose run --rm seed`.
const BUNDLED = 'navimow';

/** A panel of the bundled dashboard, brought into view and found to have no failed query. */
const bundledPanel = async (dashboard: DashboardPage, title: string): Promise<Locator> => {
  const panel = dashboard.getPanelByTitle(title);
  await panel.locator.scrollIntoViewIfNeeded();
  await expect(panel.getErrorIcon(), `${title}: its query failed`).toHaveCount(0);
  return panel.locator;
};

test('the bundled dashboard shows a collector database with no query edited', async ({ gotoDashboardPage }) => {
  const dashboard = await gotoDashboardPage({ uid: BUNDLED });
  const shows = async (title: string, ...texts: Array<string | RegExp>) => {
    for (const text of texts) {
      await expect(await bundledPanel(dashboard, title), title).toContainText(text);
    }
  };
  // A panel that draws on a canvas has one only once it has data to draw.
  const draws = async (title: string) =>
    expect((await bundledPanel(dashboard, title)).locator('canvas').first(), title).toBeVisible();

  // On its default range, the last 24 hours: the Job that ended some hours ago, and the dock since.
  await shows('State', 'Docked');
  await shows('Battery', '90%');
  await shows('Job progress', '100%');
  await shows('Area', '653 m²');
  await shows('Zone progress', 'Zone 1', 'Zone 6', 'Zone 7', 'Zone 9', 'Zone 10', 'Zone 11');
  await shows('Jobs', 'Completed', '03:13:53');
  await shows('State timeline', 'Mowing', 'Returning', 'Docked');
  await draws('Battery level');
  await shows('Job progress over time', /\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ/);
  // No dashboard can know where an owner's dock stands: until they say, the map asks.
  await shows('Lawn', "Set the Dock origin's latitude and longitude");

  // The Season row keeps its own ranges, so it holds the Job of a week ago as well.
  await draws('Area per week');
  await shows('Time on Jobs', '6.5 h');
  await shows('Errors', 'Error');
});

test('the bundled dashboard is quiet over a time with no Job in it', async ({ gotoDashboardPage, page }) => {
  // Three days ago, between the two Jobs: a day the mower sat at the dock, as it does all winter.
  const dashboard = await gotoDashboardPage({ uid: BUNDLED, timeRange: { from: 'now-4d', to: 'now-3d' } });

  // The mower was docked throughout, which it last said the week before.
  await expect(await bundledPanel(dashboard, 'State')).toContainText('Docked');
  await expect(await bundledPanel(dashboard, 'State timeline')).toContainText('Docked');
  for (const title of ['Job progress', 'Area', 'Zone progress', 'Jobs', 'Battery level', 'Job progress over time']) {
    await expect(await bundledPanel(dashboard, title), title).toContainText('No data');
  }
  await bundledPanel(dashboard, 'Lawn');
  const job = page.getByTestId('data-testid Dashboard template variables submenu Label Job');
  await expect(job.getByTestId('icon-exclamation-triangle'), 'the Job variable reports an error').toHaveCount(0);
});

test('a Job picked from the bundled dashboard’s table is the one the dashboard shows', async ({
  gotoDashboardPage,
  page,
}) => {
  // Over both Jobs: with the Job variable on All, the tiles speak of the newer.
  const dashboard = await gotoDashboardPage({ uid: BUNDLED, timeRange: { from: 'now-8d', to: 'now' } });
  const jobs = await bundledPanel(dashboard, 'Jobs');
  await expect(jobs.getByRole('link')).toHaveCount(2);

  await jobs.getByRole('link').last().click();

  // The older Job, named by when it started: the only one with an error in it.
  await expect(page).toHaveURL(/var-job=\d{4}-\d\d-\d\dT[\d:%A]+Z/);
  await expect(await bundledPanel(dashboard, 'State timeline')).toContainText('Error');
  const progress = await bundledPanel(dashboard, 'Job progress over time');
  await expect(progress.getByText(/^\d{4}-.*Z$/)).toHaveCount(1);
});

test('once its Dock origin is set, the bundled dashboard draws the Trail of the Job', async ({
  gotoDashboardPage,
  page,
}) => {
  // The dashboard as an owner saves it with their Lawn: the same file, with a Dock origin in the
  // map's options.
  const file = path.join(__dirname, '..', '..', 'dashboards', 'navimow-postgresql.json');
  const bundled = JSON.parse(readFileSync(file, 'utf8'));
  const lawns = { '*': { dockOrigin: { lat: 59.964, lon: 10.672, rotation: 0 } } };
  const withLawn = {
    ...bundled,
    uid: 'navimow-with-lawn',
    title: 'Navimow, with a Lawn',
    panels: bundled.panels.map((panel: { title: string; options?: object }) =>
      panel.title === 'Lawn' ? { ...panel, options: { ...panel.options, lawns } } : panel
    ),
  };
  const saved = await page.request.post('/api/dashboards/db', { data: { dashboard: withLawn, overwrite: true } });
  expect(saved.ok(), await saved.text()).toBe(true);

  const map = await bundledPanel(await gotoDashboardPage({ uid: withLawn.uid }), 'Lawn');
  await expectDrawn(map);
  // The Job, and the positions sent from the dock before and after it, which belong to none.
  await expect(map.getByTestId('navimow-map')).toHaveAttribute('data-trails-drawn', '2');
  await page.request.delete(`/api/dashboards/uid/${withLawn.uid}`);
});
