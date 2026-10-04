import React, { Component, useEffect, useRef, type ReactNode } from 'react';
import { css } from '@emotion/css';
import { GPUInitializationError, Map, Marker, setWorkerUrl, type GeoJSONSource } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { framedBounds, nextFraming, type Framing } from '../model/framing';
import { mapStyle, type MapSources } from '../model/style';
import type { MowerMarker, TrailScene } from '../model/trail';
import { cameraFor, type Camera } from '../model/view';
import { PanelMessage } from './PanelMessage';

declare let __webpack_public_path__: string;
// The worker is copied into dist/ by webpack.config.ts and served same-origin, beside module.js.
setWorkerUrl(__webpack_public_path__ + 'maplibre-gl-worker.mjs');

// Until a Dock origin is calibrated there is nothing to centre on, so fit all of Norway.
const NORWAY: [[number, number], [number, number]] = [
  [4.5, 57.9],
  [31.2, 71.2],
];

// MapLibre colours attribution links but not plain text, which would inherit Grafana's light
// dark-theme text and vanish on the light attribution strip.
const container = css({ '.maplibregl-ctrl-attrib': { color: 'rgba(0, 0, 0, 0.75)' } });

const NO_WEBGL =
  'The map needs WebGL, which this browser has turned off or does not support. ' +
  'Turn on hardware acceleration in the browser settings and reload the page.';

const cameraOf = (map: Map): Camera => ({
  center: map.getCenter().toArray(),
  zoom: map.getZoom(),
  bearing: map.getBearing(),
  pitch: map.getPitch(),
});

const mowerStyles = {
  icon: css({ width: 28, height: 28, svg: { display: 'block' } }),
  lastSeen: css({
    padding: '1px 6px',
    borderRadius: 8,
    background: 'rgba(255, 255, 255, 0.9)',
    boxShadow: '0 1px 2px rgba(0, 0, 0, 0.3)',
    color: '#1f1f1f',
    font: '12px/16px sans-serif',
    whiteSpace: 'nowrap',
  }),
};

// An arrow when the heading is known, pointing up so the marker's rotation is the compass bearing.
const mowerIcon = ({ bearing, stale }: MowerMarker): string => {
  const paint = `fill="${stale ? '#6e6e6e' : '#1f1f1f'}" stroke="#fff" stroke-width="2" stroke-linejoin="round"`;
  const shape =
    bearing === undefined ? `<circle cx="12" cy="12" r="7" ${paint}/>` : `<path d="M12 2 20 21 12 17 4 21Z" ${paint}/>`;
  return `<svg viewBox="0 0 24 24" width="28" height="28">${shape}</svg>`;
};

interface Props extends MapSources {
  trail: TrailScene;
  mower?: MowerMarker;
  width: number;
  height: number;
}

/**
 * Thin adapter over MapLibre: owns exactly one map instance and draws what it is given. Its
 * children are the panel's controls, laid over the map.
 */
export const MapView: React.FC<Props & { children?: ReactNode }> = ({ children, ...props }) => (
  <WebGLBoundary width={props.width} height={props.height}>
    <div style={{ position: 'relative', width: props.width, height: props.height }}>
      <MapCanvas {...props} />
      {children}
    </div>
  </WebGLBoundary>
);

/**
 * MapLibre 6 needs WebGL 2 and throws GPUInitializationError from its constructor when it cannot get
 * a context: no hardware acceleration, WebGL 2 disabled, or too many live contexts. That becomes a
 * readable message instead of Grafana's generic panel error; anything else still propagates.
 */
class WebGLBoundary extends Component<{ width: number; height: number; children: ReactNode }, { error?: Error }> {
  state: { error?: Error } = {};

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (!error) {
      return this.props.children;
    }
    if (error instanceof GPUInitializationError) {
      return <PanelMessage width={this.props.width} height={this.props.height} text={NO_WEBGL} />;
    }
    throw error;
  }
}

/** Clears the drawn mark until the map next goes idle with the new style or data in. */
const redrawing = (element: HTMLElement | null) => element?.removeAttribute('data-map-idle');

const MapCanvas: React.FC<Props> = ({ baseMap, overlay, terrain, trail, mower, width, height }) => {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<Map | null>(null);
  const drawnTerrain = useRef(terrain);
  const lines = useRef(trail.lines);
  const framed = useRef<Framing>(undefined);
  const countPending = useRef(true);

  // New data replaces the Trail's source data only. The source exists once the style has loaded;
  // until then the style itself carries the data, topped up on load below.
  useEffect(() => {
    lines.current = trail.lines;
    countPending.current = true;
    const source = map.current?.getSource<GeoJSONSource>('trail');
    if (source) {
      redrawing(element.current);
      source.setData(trail.lines);
    }
  }, [trail.lines]);

  // Create the map on first draw, then restyle it in place: a second style set before the first
  // has loaded makes MapLibre rebuild from scratch. A change of Terrain is the exception. Terrain
  // has to be in a map's first style (see mapStyle), so the map is recreated where the last one
  // was looking; the model decides the camera (cameraFor).
  useEffect(() => {
    const style = mapStyle({ baseMap, overlay, terrain }, lines.current);
    const previous = map.current;
    redrawing(element.current);
    if (previous && drawnTerrain.current === terrain) {
      previous.setStyle(style);
      return;
    }
    const view = terrain ? 'terrain' : 'flat';
    const camera = previous ? cameraFor(view, cameraOf(previous)) : { bounds: NORWAY, ...cameraFor(view) };
    previous?.remove();
    const created = new Map({
      container: element.current!,
      style,
      ...camera,
      // Past MapLibre's default of 60, to look across a slope rather than down on it.
      maxPitch: 75,
      // Attribution is a licence obligation: never collapsed, never hideable.
      attributionControl: { compact: false },
    });
    created.on('error', (e) => console.error('[navimow-map]', e.error?.message ?? e));
    created.on('style.load', () => created.getSource<GeoJSONSource>('trail')?.setData(lines.current));
    // Marks a fully drawn map, how many Trails it drew and where it looks from, so browser tests can
    // wait for rendering to finish and see what came out. Trails are counted once per new Trail data
    // or map, so panning never pays for it; a line crossing tiles comes back once per tile, hence the ids.
    created.on('idle', () => {
      if (countPending.current) {
        countPending.current = false;
        const drawn = new Set(created.queryRenderedFeatures({ layers: ['trail'] }).map((f) => f.id));
        element.current?.setAttribute('data-trails-drawn', String(drawn.size));
      }
      const at = { ...cameraOf(created), groundElevation: created.getCameraTargetElevation() };
      element.current?.setAttribute('data-camera', JSON.stringify(at));
      element.current?.setAttribute('data-map-idle', '');
    });
    map.current = created;
    drawnTerrain.current = terrain;
    countPending.current = true;
  }, [baseMap, overlay, terrain]);

  // When to frame the Trail is the model's decision (nextFraming); this only carries it out.
  useEffect(() => {
    // A panel with no size yet cannot be framed; recording it as framed would mean it never is.
    if (width <= 0 || height <= 0) {
      return;
    }
    const next = nextFraming(trail, framed.current);
    if (map.current && trail.origin && next) {
      // MapLibre learns of a new panel size from a throttled observer, which may not have run yet;
      // fitting against the old size would frame the Trail wrongly, and for good.
      map.current.resize();
      framed.current = next;
      // Padding is capped so a small panel still has room left to fit into.
      map.current.fitBounds(framedBounds(next, trail.origin), {
        padding: Math.min(20, width / 4, height / 4),
        duration: 0,
      });
    }
  }, [trail, width, height]);

  // The rotating icon and its age label are separate markers, so the label stays upright.
  useEffect(() => {
    if (!map.current || !mower) {
      return;
    }
    const icon = document.createElement('div');
    icon.className = mowerStyles.icon;
    icon.innerHTML = mowerIcon(mower);
    icon.setAttribute('role', 'img');
    icon.setAttribute('aria-label', mower.stale ? `Mower, ${mower.lastSeen.toLowerCase()}` : 'Mower');
    const markers = [
      new Marker({
        element: icon,
        rotation: mower.bearing ?? 0,
        rotationAlignment: 'map',
        opacity: mower.stale ? 0.75 : 1,
      }),
    ];
    if (mower.stale) {
      const label = document.createElement('div');
      label.className = mowerStyles.lastSeen;
      label.textContent = mower.lastSeen;
      markers.push(new Marker({ element: label, anchor: 'top', offset: [0, 16] }));
    }
    markers.forEach((m) => m.setLngLat(mower.position).addTo(map.current!));
    return () => markers.forEach((m) => m.remove());
    // A change of Terrain recreates the map, which needs its markers again.
  }, [mower, terrain]);

  // Browsers keep only about eight WebGL contexts, so release this one with the panel.
  useEffect(
    () => () => {
      map.current?.remove();
      map.current = null;
      // A remount (React's strict mode does one) gets a fresh map, which must be framed and counted again.
      framed.current = undefined;
      countPending.current = true;
    },
    []
  );

  // MapLibre follows container size changes itself (trackResize), so the panel's size is all it needs.
  return <div ref={element} className={container} style={{ width, height }} data-testid="navimow-map" />;
};
