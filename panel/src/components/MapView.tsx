import React, { Component, useEffect, useRef, type ReactNode } from 'react';
import { css } from '@emotion/css';
import {
  GPUInitializationError,
  Map,
  setWorkerUrl,
  type RasterSourceSpecification,
  type StyleSpecification,
} from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
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

const styleFor = (baseMap: RasterSourceSpecification): StyleSpecification => ({
  version: 8,
  sources: { base: baseMap },
  layers: [{ id: 'base', type: 'raster', source: 'base' }],
});

interface Props {
  baseMap: RasterSourceSpecification;
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

const MapCanvas: React.FC<Props> = ({ baseMap, width, height }) => {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<Map | null>(null);

  // Create the map on first draw, then restyle it in place: a second style set before the first
  // has loaded makes MapLibre rebuild from scratch.
  useEffect(() => {
    if (map.current) {
      // Not drawn again until the new style's tiles are in.
      element.current?.removeAttribute('data-map-idle');
      map.current.setStyle(styleFor(baseMap));
      return;
    }
    map.current = new Map({
      container: element.current!,
      style: styleFor(baseMap),
      bounds: NORWAY,
      // Attribution is a licence obligation: never collapsed, never hideable.
      attributionControl: { compact: false },
    });
    map.current.on('error', (e) => console.error('[navimow-map]', e.error?.message ?? e));
    // Marks a fully drawn map, so browser tests can wait for rendering to finish.
    map.current.on('idle', () => element.current?.setAttribute('data-map-idle', ''));
  }, [baseMap]);

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
