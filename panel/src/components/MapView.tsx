import React, { useEffect, useRef, useState } from 'react';
import { css } from '@emotion/css';
import { Map, setWorkerUrl, type RasterSourceSpecification, type StyleSpecification } from 'maplibre-gl';
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

// Probed before constructing the map, so a machine without hardware acceleration gets a message
// rather than a blank panel. The probe's context is released at once: browsers cap live contexts.
const hasWebGL = (): boolean => {
  const gl =
    document.createElement('canvas').getContext('webgl2') ?? document.createElement('canvas').getContext('webgl');
  gl?.getExtension('WEBGL_lose_context')?.loseContext();
  return gl !== null;
};

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
export const MapView: React.FC<Props> = ({ baseMap, width, height }) => {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<Map | null>(null);
  const [webgl] = useState(hasWebGL);

  // Create the map on first draw, then restyle it in place: a second style set before the first
  // has loaded makes MapLibre rebuild from scratch.
  useEffect(() => {
    if (!webgl) {
      return;
    }
    if (map.current) {
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
  }, [webgl, baseMap]);

  // Browsers keep only about eight WebGL contexts, so release this one with the panel.
  useEffect(
    () => () => {
      map.current?.remove();
      map.current = null;
    },
    []
  );

  if (!webgl) {
    return <PanelMessage width={width} height={height} text={NO_WEBGL} />;
  }
  // MapLibre follows container size changes itself (trackResize), so the panel's size is all it needs.
  return <div ref={element} className={container} style={{ width, height }} data-testid="navimow-map" />;
};
