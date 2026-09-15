import type { Feature, LineString, Point } from 'geojson';
// Synthetic boustrophedon mowing Trail: ~30 lanes, 1.5 m apart, 40 m long, rotated 20°,
// in local metres from a fake Dock origin, converted to lon/lat.
const DOCK = { lon: 10.672, lat: 59.964 };
const ROT = (20 * Math.PI) / 180;

function toLonLat(x: number, y: number): [number, number] {
  const e = x * Math.cos(ROT) - y * Math.sin(ROT);
  const n = x * Math.sin(ROT) + y * Math.cos(ROT);
  const dLat = n / 111320;
  const dLon = e / (111320 * Math.cos((DOCK.lat * Math.PI) / 180));
  return [DOCK.lon + dLon, DOCK.lat + dLat];
}

const pts: Array<[number, number]> = [];
for (let lane = 0; lane < 30; lane++) {
  const y = lane * 1.5;
  const xs = lane % 2 === 0 ? [0, 40] : [40, 0];
  for (const x of xs) {
    pts.push(toLonLat(x, y));
  }
}

export const trail: Feature<LineString> = {
  type: 'Feature',
  properties: {},
  geometry: { type: 'LineString', coordinates: pts },
};
export const mower: Feature<Point> = {
  type: 'Feature',
  properties: { heading: 20 + 90 },
  geometry: { type: 'Point', coordinates: pts[pts.length - 1] },
};
export const dock: Feature<Point> = {
  type: 'Feature',
  properties: {},
  geometry: { type: 'Point', coordinates: toLonLat(0, 0) },
};
export const center = toLonLat(20, 22);
