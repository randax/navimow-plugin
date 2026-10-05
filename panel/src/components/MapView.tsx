import React, { Component, useEffect, useRef, useState, type ReactNode } from 'react';
import { css } from '@emotion/css';
import type { GrafanaTheme2 } from '@grafana/data';
import { useStyles2 } from '@grafana/ui';
import { GPUInitializationError, Map, Marker, setWorkerUrl, type GeoJSONSource, type PointLike } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { DockOrigin } from '../model/dockOrigin';
import { framedBounds, movesView, nextFraming, type Framing } from '../model/framing';
import type { Detail } from '../model/hover';
import type { BoundaryFeatures } from '../model/lawn';
import { LAYERS, mapStyle, type Layer, type MapSources } from '../model/style';
import type { MowerMarker, TrailScene } from '../model/trail';
import { cameraFor, type Camera, type View } from '../model/view';
import { MapControls } from './MapControls';
import { MapTooltip } from './MapTooltip';
import { PanelMessage } from './PanelMessage';

declare let __webpack_public_path__: string;
// The worker is copied into dist/ by webpack.config.ts and served same-origin, beside module.js.
setWorkerUrl(__webpack_public_path__ + 'maplibre-gl-worker.mjs');

/** Until a Dock origin is calibrated there is nothing to centre on, so fit all of Norway. */
export const NORWAY: [[number, number], [number, number]] = [
  [4.5, 57.9],
  [31.2, 71.2],
];

/**
 * MapLibre colours attribution links but not plain text, which would inherit Grafana's light
 * dark-theme text and vanish on the light attribution strip.
 */
export const ATTRIBUTION_STYLE = { '.maplibregl-ctrl-attrib': { color: 'rgba(0, 0, 0, 0.75)' } };
const container = css(ATTRIBUTION_STYLE);

/** Logs a map's errors under the plugin's tag, which the browser tests collect. */
export const reportErrors = (map: Map) =>
  map.on('error', (e) => console.error('[navimow-map]', e.error?.message ?? e));

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

/** What the pointer is on: a drawn Trail, by its line's id and the place on it, or a Zone of the Boundary. */
export type MapHit = { layer: 'trail'; trail: number; at: [number, number] } | { layer: 'zone'; zone: string };

interface Props extends MapSources {
  view: View;
  trail: TrailScene;
  boundary: BoundaryFeatures;
  mower?: MowerMarker;
  width: number;
  height: number;
  /** The layers the owner has hidden from the panel. */
  hidden: readonly Layer[];
  onHidden: (hidden: Layer[]) => void;
  /** Whether the view keeps the mower in its middle. */
  following: boolean;
  onFollow: (following: boolean) => void;
  /** What to tell about the thing under the pointer, if anything. */
  detailOf: (hit: MapHit) => Detail | undefined;
  /** Whether a click there selects something, which the pointer then shows. */
  selects: (hit: MapHit) => boolean;
  onSelect: (hit: MapHit) => void;
}

// A Trail is a line two pixels wide; this much to either side still counts as on it.
const REACH = 4;

/** What is drawn under a point of the map, the Trail before the Zone it crosses. */
const hitAt = (map: Map, { x, y }: { x: number; y: number }): MapHit | undefined => {
  // Asking for a layer the style has yet to load is an error to MapLibre.
  if (!map.getLayer('trail') || !map.getLayer('boundary-fill')) {
    return undefined;
  }
  const around: [PointLike, PointLike] = [
    [x - REACH, y - REACH],
    [x + REACH, y + REACH],
  ];
  const [trail] = map.queryRenderedFeatures(around, { layers: ['trail'] });
  if (typeof trail?.id === 'number') {
    return { layer: 'trail', trail: trail.id, at: map.unproject([x, y]).toArray() };
  }
  const zone = map.queryRenderedFeatures([x, y], { layers: ['boundary-fill'] }).find((f) => f.properties.kind === 'zone');
  return zone && { layer: 'zone', zone: String(zone.properties.id) };
};

/**
 * The same object for as long as its value is the same. Grafana hands the panel a fresh copy of all
 * its options whenever one changes, so a source is a new object even when nothing about it did; by
 * value, the map is restyled only for a source that differs, and recreated only for a new Terrain.
 */
export const useByValue = <T,>(value: T): T => {
  const json = JSON.stringify(value);
  const [kept, keep] = useState({ value, json });
  if (kept.json !== json) {
    keep({ value, json });
    return value;
  }
  return kept.value;
};

/**
 * Thin adapter over MapLibre: owns exactly one map instance and draws what it is given. Its
 * children are the panel's controls, laid over the map.
 */
export const MapView: React.FC<Props & { children?: ReactNode }> = ({ children, ...props }) => {
  const styles = useStyles2(getStyles);
  const sources: MapSources = {
    baseMap: useByValue(props.baseMap),
    overlay: useByValue(props.overlay),
    // Flat is the map without its Terrain.
    terrain: useByValue(props.view === 'terrain' ? props.terrain : undefined),
  };
  const hidden = useByValue(props.hidden);
  return (
    <WebGLBoundary width={props.width} height={props.height}>
      <div style={{ position: 'relative', width: props.width, height: props.height }}>
        <MapCanvas {...props} {...sources} hidden={hidden} />
        <div className={styles.topLeft}>{children}</div>
      </div>
    </WebGLBoundary>
  );
};

const getStyles = (theme: GrafanaTheme2) => ({
  // Beside the controls, which keep the right edge to themselves.
  topLeft: css({
    position: 'absolute',
    top: theme.spacing(1),
    left: theme.spacing(1),
    maxWidth: `calc(100% - ${theme.spacing(8)})`,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'flex-start',
    gap: theme.spacing(1),
  }),
});

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

/**
 * Puts a framing in view. A framing the panel makes for itself lands at once, as a refresh must not
 * look like a journey; one the owner asks for is flown to, so they see where it takes them.
 */
const fit = (
  map: Map,
  framing: Framing,
  origin: DockOrigin,
  { width, height, animate = false }: { width: number; height: number; animate?: boolean }
) =>
  map.fitBounds(framedBounds(framing, origin), {
    // Padding is capped so a small panel still has room left to fit into.
    padding: Math.min(20, width / 4, height / 4),
    ...(!animate && { duration: 0 }),
  });

/** Clears the drawn mark until the map next goes idle with the new style or data in. */
const redrawing = (element: HTMLElement | null) => element?.removeAttribute('data-map-idle');

const MapCanvas: React.FC<Props> = ({
  baseMap,
  overlay,
  terrain,
  view,
  trail,
  boundary,
  mower,
  width,
  height,
  hidden,
  onHidden,
  following,
  onFollow,
  detailOf,
  selects,
  onSelect,
}) => {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<Map | null>(null);
  const drawn = useRef({ view, terrain });
  const lines = useRef(trail.lines);
  const rings = useRef(boundary);
  const topUp = useRef(false);
  const framed = useRef<Framing>(undefined);
  const countPending = useRef(true);
  const [bearing, setBearing] = useState(0);
  const [hover, setHover] = useState<{ at: { x: number; y: number }; detail: Detail }>();
  // The map's listeners are added once per map, and must answer with the panel's latest data.
  const pointer = useRef({ detailOf, selects, onSelect });
  useEffect(() => {
    pointer.current = { detailOf, selects, onSelect };
  });

  // New data replaces a source's data only. The source exists once the style has loaded; until
  // then the style carries the data it was made with, topped up on load below.
  const replaceData = (id: 'trail' | 'boundary', data: GeoJSON.FeatureCollection) => {
    const source = map.current?.getSource<GeoJSONSource>(id);
    if (source) {
      redrawing(element.current);
      source.setData(data);
    } else if (map.current) {
      topUp.current = true;
    }
  };
  useEffect(() => {
    lines.current = trail.lines;
    countPending.current = true;
    replaceData('trail', trail.lines);
  }, [trail.lines]);
  useEffect(() => {
    rings.current = boundary;
    replaceData('boundary', boundary);
  }, [boundary]);

  // Create the map on first draw, then restyle it in place: a second style set before the first
  // has loaded makes MapLibre rebuild from scratch. A change of Terrain is the exception. Terrain
  // has to be in a map's first style (see mapStyle), so the map is recreated where the last one
  // was looking; the model decides the camera (cameraFor).
  useEffect(() => {
    const style = mapStyle({ baseMap, overlay, terrain }, lines.current, rings.current, hidden);
    const previous = map.current;
    redrawing(element.current);
    // A restyle can hide the Trail or show it again, so what is drawn is counted afresh.
    countPending.current = true;
    if (previous && drawn.current.terrain === terrain) {
      previous.setStyle(style);
      return;
    }
    const camera = previous
      ? cameraFor(view, { view: drawn.current.view, camera: cameraOf(previous) })
      : { bounds: NORWAY, ...cameraFor(view) };
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
    reportErrors(created);
    // Only for data that arrived while the style was loading. MapLibre fires this after every restyle
    // in place too, where the Trail is already there and sending it again would re-tile it for nothing.
    topUp.current = false;
    created.on('style.load', () => {
      if (topUp.current) {
        topUp.current = false;
        created.getSource<GeoJSONSource>('trail')?.setData(lines.current);
        created.getSource<GeoJSONSource>('boundary')?.setData(rings.current);
      }
    });
    // Marks a fully drawn map, how many Trails it drew and where it looks from, so browser tests can
    // wait for rendering to finish and see what came out. Trails are counted once per new Trail data
    // or map, so panning never pays for it; a line crossing tiles comes back once per tile, hence the ids.
    created.on('idle', () => {
      if (countPending.current) {
        countPending.current = false;
        const trails = new Set(created.queryRenderedFeatures({ layers: ['trail'] }).map((f) => f.id));
        element.current?.setAttribute('data-trails-drawn', String(trails.size));
      }
      const at = { ...cameraOf(created), groundElevation: created.getCameraTargetElevation() };
      element.current?.setAttribute('data-camera', JSON.stringify(at));
      element.current?.setAttribute('data-map-idle', '');
    });
    created.on('rotate', () => setBearing(created.getBearing()));
    created.on('mousemove', ({ point, originalEvent }) => {
      // With a button held the pointer is dragging the map, not asking about it.
      if (originalEvent.buttons !== 0) {
        return;
      }
      const hit = hitAt(created, point);
      const detail = hit && pointer.current.detailOf(hit);
      setHover(detail && { at: { x: originalEvent.clientX, y: originalEvent.clientY }, detail });
      created.getCanvas().style.cursor = hit && pointer.current.selects(hit) ? 'pointer' : '';
    });
    // A detail belongs to where the pointer is: gone once it leaves the map, or drags the map from under it.
    created.on('mouseout', () => setHover(undefined));
    created.on('movestart', () => setHover(undefined));
    created.on('click', ({ point }) => {
      const hit = hitAt(created, point);
      if (hit) {
        pointer.current.onSelect(hit);
      }
    });
    map.current = created;
    drawn.current = { view, terrain };
    setBearing(created.getBearing());
  }, [baseMap, overlay, terrain, view, hidden]);


  // When to frame the Trail, and whether that moves a view that follows the mower, are the model's
  // decisions (nextFraming, movesView); this only carries them out.
  useEffect(() => {
    // A panel with no size yet cannot be framed; recording it as framed would mean it never is.
    if (width <= 0 || height <= 0) {
      return;
    }
    const next = nextFraming(trail, framed.current);
    if (map.current && trail.origin && next) {
      if (movesView(next, framed.current, following)) {
        // MapLibre learns of a new panel size from a throttled observer, which may not have run yet;
        // fitting against the old size would frame the Trail wrongly, and for good.
        map.current.resize();
        fit(map.current, next, trail.origin, { width, height });
      }
      framed.current = next;
    }
  }, [trail, width, height, following]);

  // Following keeps the mower in the middle and leaves the zoom to the owner. It moves the view
  // only when the mower does, so a look around between two positions is not snatched back.
  const [mowerLon, mowerLat] = mower?.position ?? [];
  useEffect(() => {
    if (following && mowerLon !== undefined && mowerLat !== undefined) {
      map.current?.easeTo({ center: [mowerLon, mowerLat] });
    }
  }, [following, mowerLon, mowerLat]);

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

  const whole = nextFraming(trail, undefined);
  // MapLibre follows container size changes itself (trackResize), so the panel's size is all it needs.
  return (
    <>
      <div ref={element} className={container} style={{ width, height }} data-testid="navimow-map" />
      <MapControls
        bearing={bearing}
        following={following}
        layers={LAYERS.map((l) => l.id).filter((id) => id !== 'boundary' || boundary.features.length > 0)}
        hidden={hidden}
        canFit={whole !== undefined}
        canFollow={mower !== undefined}
        onZoom={(by) => (by > 0 ? map.current?.zoomIn() : map.current?.zoomOut())}
        onNorth={() => map.current?.resetNorth()}
        onFit={() => {
          if (map.current && whole && trail.origin) {
            framed.current = whole;
            fit(map.current, whole, trail.origin, { width, height, animate: true });
          }
        }}
        onFollow={onFollow}
        onHidden={onHidden}
      />
      {hover && <MapTooltip at={hover.at} detail={hover.detail} />}
    </>
  );
};
