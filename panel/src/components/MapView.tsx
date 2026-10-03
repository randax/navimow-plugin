import React, { Component, useEffect, useRef, type ReactNode } from 'react';
import { css } from '@emotion/css';
import {
  GPUInitializationError,
  Map,
  Marker,
  setWorkerUrl,
  type GeoJSONSource,
  type RasterSourceSpecification,
  type StyleSpecification,
} from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { MowerMarker, TrailScene } from '../model/trail';
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

// The Trail is part of the style, so a Base map switch keeps it and a refresh only diffs its data.
const styleFor = (baseMap: RasterSourceSpecification, trail: TrailScene['lines']): StyleSpecification => ({
  version: 8,
  sources: { base: baseMap, trail: { type: 'geojson', data: trail } },
  layers: [
    { id: 'base', type: 'raster', source: 'base' },
    {
      id: 'trail',
      type: 'line',
      source: 'trail',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': ['get', 'colour'], 'line-width': 2, 'line-opacity': 0.9 },
    },
  ],
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

interface Props {
  baseMap: RasterSourceSpecification;
  trail: TrailScene;
  width: number;
  height: number;
}

/** Thin adapter over MapLibre: owns exactly one map instance and draws what it is given. */
export const MapView: React.FC<Props> = (props) => (
  <WebGLBoundary width={props.width} height={props.height}>
    <MapCanvas {...props} />
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

const MapCanvas: React.FC<Props> = ({ baseMap, trail, width, height }) => {
  const { mower } = trail;
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<Map | null>(null);
  const lines = useRef(trail.lines);
  const fitted = useRef(false);

  // New data replaces the Trail's source data only. The source exists once the style has loaded;
  // until then the style itself carries the data, topped up on load below.
  useEffect(() => {
    lines.current = trail.lines;
    const source = map.current?.getSource<GeoJSONSource>('trail');
    if (source) {
      redrawing(element.current);
      source.setData(trail.lines);
    }
  }, [trail.lines]);

  // Create the map on first draw, then restyle it in place: a second style set before the first
  // has loaded makes MapLibre rebuild from scratch.
  useEffect(() => {
    if (map.current) {
      redrawing(element.current);
      map.current.setStyle(styleFor(baseMap, lines.current));
      return;
    }
    const created = new Map({
      container: element.current!,
      style: styleFor(baseMap, lines.current),
      bounds: NORWAY,
      // Attribution is a licence obligation: never collapsed, never hideable.
      attributionControl: { compact: false },
    });
    created.on('error', (e) => console.error('[navimow-map]', e.error?.message ?? e));
    created.on('style.load', () => created.getSource<GeoJSONSource>('trail')?.setData(lines.current));
    // Marks a fully drawn map, and how many Trails it drew, so browser tests can wait for rendering
    // to finish and see what came out. A line crossing tiles comes back once per tile, hence the ids.
    created.on('idle', () => {
      const drawn = new Set(created.queryRenderedFeatures({ layers: ['trail'] }).map((f) => f.id));
      element.current?.setAttribute('data-trails-drawn', String(drawn.size));
      element.current?.setAttribute('data-map-idle', '');
    });
    map.current = created;
  }, [baseMap]);

  // Frame the Trail when it first appears, and never again: a refresh must not undo the owner's panning.
  useEffect(() => {
    if (map.current && trail.bounds && !fitted.current) {
      fitted.current = true;
      // Padding is capped so a small panel still has room left to fit into.
      map.current.fitBounds(trail.bounds, { padding: Math.min(40, width / 4, height / 4), maxZoom: 20, duration: 0 });
    }
  }, [trail.bounds, width, height]);

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
  }, [mower]);

  // Browsers keep only about eight WebGL contexts, so release this one with the panel.
  useEffect(
    () => () => {
      map.current?.remove();
      map.current = null;
    },
    []
  );

  // MapLibre follows container size changes itself (trackResize), so the panel's size is all it needs.
  return <div ref={element} className={container} style={{ width, height }} data-testid="navimow-map" />;
};
