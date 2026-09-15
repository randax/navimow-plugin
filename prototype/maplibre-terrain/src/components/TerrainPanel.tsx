import React, { useEffect, useRef, useState } from 'react';
import { PanelProps } from '@grafana/data';
import { Map, StyleSpecification, setWorkerUrl, ErrorEvent } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { TerrainOptions } from '../types';
import { trail, mower, dock, center } from '../trail';

declare let __webpack_public_path__: string;
setWorkerUrl(__webpack_public_path__ + 'maplibre-gl-worker.mjs');

const style: StyleSpecification = {
  version: 8,
  sources: {
    kartverket: {
      type: 'raster',
      tiles: ['https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png'],
      tileSize: 256,
      maxzoom: 18,
      attribution: '<a href="https://www.kartverket.no/">© Kartverket</a>',
    },
    terrain: {
      type: 'raster-dem',
      tiles: ['https://tiles.mapterhorn.com/{z}/{x}/{y}.webp'],
      encoding: 'terrarium',
      tileSize: 512,
      maxzoom: 16,
      attribution: '© Mapterhorn, © Kartverket',
    },
    trail: { type: 'geojson', data: trail },
    mower: { type: 'geojson', data: mower },
    dock: { type: 'geojson', data: dock },
  },
  layers: [
    { id: 'base', type: 'raster', source: 'kartverket' },
    { id: 'hillshade', type: 'hillshade', source: 'terrain', paint: { 'hillshade-exaggeration': 0.4 } },
    {
      id: 'trail',
      type: 'line',
      source: 'trail',
      paint: { 'line-color': '#2ecc40', 'line-width': 3, 'line-opacity': 0.75 },
      layout: { 'line-join': 'round', 'line-cap': 'round' },
    },
    { id: 'dock', type: 'circle', source: 'dock', paint: { 'circle-radius': 6, 'circle-color': '#ff851b' } },
    { id: 'mower', type: 'circle', source: 'mower', paint: { 'circle-radius': 7, 'circle-color': '#0074d9', 'circle-stroke-width': 2, 'circle-stroke-color': '#fff' } },
  ],
};

export const TerrainPanel: React.FC<PanelProps<TerrainOptions>> = ({ options, width, height }) => {
  const el = useRef<HTMLDivElement>(null);
  const mapRef = useRef<Map | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!el.current) {
      return;
    }
    const t0 = performance.now();
    try {
      const map = new Map({
        container: el.current,
        style: options.terrain ? { ...style, terrain: { source: 'terrain', exaggeration: options.exaggeration } } : style,
        center,
        zoom: 18,
        pitch: options.pitch,
        bearing: 20,
        maxPitch: 75,
      });
      map.once('idle', () => console.warn(`[terrain-proto] first idle after ${Math.round(performance.now() - t0)} ms`));
      map.on('error', (e: ErrorEvent) => console.warn('[terrain-proto] map error', e.error?.message ?? e));
      mapRef.current = map;
    } catch (e) {
      setError(String(e));
    }
    return () => {
      mapRef.current?.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    const apply = () => {
      map.setTerrain(options.terrain ? { source: 'terrain', exaggeration: options.exaggeration } : null);
      map.setPitch(options.pitch);
    };
    if (map.isStyleLoaded()) {
      apply();
    } else {
      map.once('load', apply);
    }
  }, [options.terrain, options.exaggeration, options.pitch]);

  useEffect(() => {
    mapRef.current?.resize();
  }, [width, height]);

  if (error) {
    return <div style={{ padding: 8 }}>Map failed to initialise: {error}</div>;
  }
  return <div ref={el} style={{ width, height }} />;
};
