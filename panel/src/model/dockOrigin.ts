/**
 * Where the mower's local frame sits on Earth. The mower reports metres from its dock on a
 * right-handed frame (y a quarter turn counter-clockwise from x, theta counter-clockwise from x;
 * confirmed on a real capture in #6), with no relation to north. `rotation` is the compass bearing
 * of the local x-axis: degrees clockwise from north.
 */
export interface DockOrigin {
  lat: number;
  lon: number;
  rotation: number;
}

export type DockOriginOptions = Partial<DockOrigin>;

// WGS84. A garden is small enough to treat as flat around the dock, using the ellipsoid's
// curvature there: well under a millimetre of error across a hundred metres.
const A = 6378137;
const E2 = 0.00669437999014;
const RAD = Math.PI / 180;

/** Local metres from the dock, as [longitude, latitude]. */
export function toLonLat({ lat, lon, rotation }: DockOrigin, x: number, y: number): [number, number] {
  const b = rotation * RAD;
  const east = x * Math.sin(b) - y * Math.cos(b);
  const north = x * Math.cos(b) + y * Math.sin(b);
  const w = 1 - E2 * Math.sin(lat * RAD) ** 2;
  const metresPerDegreeLat = (A * (1 - E2) * RAD) / w ** 1.5;
  const metresPerDegreeLon = (A * Math.cos(lat * RAD) * RAD) / Math.sqrt(w);
  return [lon + east / metresPerDegreeLon, lat + north / metresPerDegreeLat];
}

/** The compass bearing, in [0, 360), of a mower heading `theta` radians counter-clockwise from its x-axis. */
export function headingBearing({ rotation }: DockOrigin, theta: number): number {
  return (((rotation - theta / RAD) % 360) + 360) % 360;
}

// Options are typed in as plain fields, or edited as panel JSON, so anything can arrive.
export function resolveDockOrigin({ lat, lon, rotation = 0 }: DockOriginOptions = {}):
  { origin: DockOrigin } | { problem: string } {
  if (typeof lat !== 'number' || typeof lon !== 'number') {
    return { problem: "Set the Dock origin's latitude and longitude in the panel options to place the Trail." };
  }
  if (!(Math.abs(lat) <= 85 && Math.abs(lon) <= 180 && Number.isFinite(rotation))) {
    return {
      problem:
        'The Dock origin needs a latitude from -85 to 85, a longitude from -180 to 180 and a rotation in degrees.',
    };
  }
  return { origin: { lat, lon, rotation } };
}
