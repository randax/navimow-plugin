import React, { useEffect, useRef, useState } from 'react';
import { css } from '@emotion/css';
import type { FeatureCollection, LineString, Point } from 'geojson';
import { Map, Marker, type GeoJSONSource, type MapMouseEvent } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { DockOrigin } from '../model/dockOrigin';
import type { Click, Draft } from '../model/drawing';
import { framedBounds, nextFraming } from '../model/framing';
import { handlePosition, rotationTowards, type BoundaryFeatures } from '../model/lawn';
import { mapStyle, type MapSources } from '../model/style';
import type { TrailScene } from '../model/trail';
import { ATTRIBUTION_STYLE, NORWAY, reportErrors, useByValue } from './MapView';

interface Props extends Omit<MapSources, 'terrain'> {
  origin?: DockOrigin;
  trail: TrailScene;
  boundary: BoundaryFeatures;
  draft: Draft;
  drawing: boolean;
  onDock: (lat: number, lon: number) => void;
  onRotation: (degrees: number) => void;
  onClick: (click: Click) => void;
  onDoubleClick: () => void;
}

// Close enough to see a dock against a lawn on the national map's finest tiles.
const DOCK_ZOOM = 18;

const styles = {
  map: css({ width: '100%', height: '100%', ...ATTRIBUTION_STYLE }),
  dock: css({
    width: 22,
    height: 22,
    borderRadius: '50%',
    background: '#1f1f1f',
    border: '3px solid #fff',
    boxShadow: '0 1px 4px rgba(0, 0, 0, 0.5)',
    cursor: 'grab',
  }),
  handle: css({
    width: 16,
    height: 16,
    borderRadius: '50%',
    background: '#fff',
    border: '3px solid #1F60C4',
    boxShadow: '0 1px 4px rgba(0, 0, 0, 0.5)',
    cursor: 'grab',
  }),
};

const AXIS_COLOUR = '#1F60C4';
const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] };

const axisLine = (origin: DockOrigin): FeatureCollection<LineString> => ({
  type: 'FeatureCollection',
  features: [
    {
      type: 'Feature',
      properties: {},
      geometry: { type: 'LineString', coordinates: [[origin.lon, origin.lat], handlePosition(origin)] },
    },
  ],
});

const draftFeatures = ({ vertices }: Draft): FeatureCollection<LineString | Point> => ({
  type: 'FeatureCollection',
  features: [
    ...(vertices.length > 1
      ? [{ type: 'Feature' as const, properties: {}, geometry: { type: 'LineString' as const, coordinates: vertices } }]
      : []),
    ...vertices.map((v) => ({ type: 'Feature' as const, properties: {}, geometry: { type: 'Point' as const, coordinates: v } })),
  ],
});

const marker = (className: string, label: string): Marker => {
  const element = document.createElement('div');
  element.className = className;
  element.setAttribute('role', 'img');
  element.setAttribute('aria-label', label);
  return new Marker({ element, draggable: true });
};

/**
 * The drawer's own map. Flat, whatever the panel shows: with Terrain the dock marker would float
 * above the ground it is dragged over, and a lawn is judged in plan. One instance for as long as
 * the drawer is open and its sources are the same by value, removed with it.
 */
export const CalibrationMap: React.FC<Props> = (props) => {
  const { origin, trail, boundary, draft, drawing } = props;
  // Grafana hands over fresh option objects on every change; the map is remade only for a source
  // that differs.
  const baseMap = useByValue(props.baseMap);
  const overlay = useByValue(props.overlay);
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<Map | null>(null);
  const [ready, setReady] = useState(false);
  const framed = useRef(false);
  // The newest props, read by handlers that are bound once.
  const latest = useRef(props);
  useEffect(() => {
    latest.current = props;
  });
  const dock = useRef<Marker | null>(null);
  const handle = useRef<Marker | null>(null);

  useEffect(() => {
    const scene = latest.current.trail;
    const start = latest.current.origin;
    const framing = start && nextFraming(scene, undefined);
    const created = new Map({
      container: element.current!,
      style: mapStyle({ baseMap, overlay }, scene.lines, latest.current.boundary),
      ...(framing && start
        ? { bounds: framedBounds(framing, start), fitBoundsOptions: { padding: 40 } }
        : start
          ? { center: [start.lon, start.lat], zoom: DOCK_ZOOM }
          : { bounds: NORWAY }),
      attributionControl: { compact: false },
    });
    framed.current = start !== undefined;
    reportErrors(created);
    // Marks a fully drawn map, so browser tests can wait for it before they drag anything on it.
    created.on('idle', () => element.current?.setAttribute('data-map-idle', ''));
    created.on('load', () => {
      created.addSource('axis', { type: 'geojson', data: EMPTY });
      created.addSource('draft', { type: 'geojson', data: EMPTY });
      created.addLayer({
        id: 'axis',
        type: 'line',
        source: 'axis',
        paint: { 'line-color': AXIS_COLOUR, 'line-width': 2, 'line-dasharray': [1, 1.5] },
      });
      created.addLayer({
        id: 'draft-line',
        type: 'line',
        source: 'draft',
        paint: { 'line-color': '#E02F44', 'line-width': 2, 'line-dasharray': [2, 1] },
      });
      created.addLayer({
        id: 'draft-points',
        type: 'circle',
        source: 'draft',
        filter: ['==', ['geometry-type'], 'Point'],
        paint: { 'circle-radius': 5, 'circle-color': '#fff', 'circle-stroke-color': '#E02F44', 'circle-stroke-width': 2 },
      });
      setReady(true);
    });
    created.on('click', (e: MapMouseEvent) =>
      latest.current.onClick({ lngLat: e.lngLat.toArray(), pixel: [e.point.x, e.point.y], time: e.originalEvent.timeStamp })
    );
    created.on('dblclick', (e: MapMouseEvent) => {
      if (latest.current.drawing) {
        e.preventDefault();
        latest.current.onDoubleClick();
      }
    });
    map.current = created;
    return () => {
      // The markers go with the map; a new map gets new ones, once it is ready.
      dock.current?.remove();
      handle.current?.remove();
      dock.current = null;
      handle.current = null;
      created.remove();
      map.current = null;
      setReady(false);
    };
  }, [baseMap, overlay]);

  // The dock and its rotation handle: made when the dock is first placed on a ready map, then moved
  // into place. A marker is placed on the map as it is added, so it needs its position first.
  useEffect(() => {
    const current = map.current;
    if (!current || !ready || !origin) {
      return;
    }
    if (!dock.current) {
      dock.current = marker(styles.dock, 'Dock').setLngLat([origin.lon, origin.lat]).addTo(current);
      dock.current.on('drag', () => {
        const { lng, lat } = dock.current!.getLngLat();
        latest.current.onDock(lat, lng);
      });
      handle.current = marker(styles.handle, 'Rotation handle').setLngLat(handlePosition(origin)).addTo(current);
      handle.current.on('drag', () => {
        const at = latest.current.origin;
        if (at) {
          latest.current.onRotation(rotationTowards(at, handle.current!.getLngLat().toArray()));
        }
      });
    } else {
      dock.current.setLngLat([origin.lon, origin.lat]);
      handle.current?.setLngLat(handlePosition(origin));
    }
    if (!framed.current) {
      framed.current = true;
      current.jumpTo({ center: [origin.lon, origin.lat], zoom: DOCK_ZOOM });
    }
  }, [origin, ready]);

  const setData = (id: string, data: FeatureCollection) => map.current?.getSource<GeoJSONSource>(id)?.setData(data);
  useEffect(() => void (ready && setData('trail', trail.lines)), [ready, trail.lines]);
  useEffect(() => void (ready && setData('boundary', boundary)), [ready, boundary]);
  useEffect(() => void (ready && setData('draft', draftFeatures(draft))), [ready, draft]);
  useEffect(() => void (ready && setData('axis', origin ? axisLine(origin) : EMPTY)), [ready, origin]);

  // While drawing, a double click closes the polygon instead of zooming, and the cursor says so.
  useEffect(() => {
    const current = map.current;
    if (!current) {
      return;
    }
    if (drawing) {
      current.doubleClickZoom.disable();
    } else {
      current.doubleClickZoom.enable();
    }
    current.getCanvas().style.cursor = drawing ? 'crosshair' : '';
  }, [drawing, ready]);

  return <div ref={element} className={styles.map} data-testid="navimow-calibration-map" />;
};
