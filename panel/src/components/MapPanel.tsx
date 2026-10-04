import React, { useEffect, useMemo, useState } from 'react';
import type { PanelProps } from '@grafana/data';
import { resolveBaseMap } from '../model/baseMap';
import { resolveOverlay } from '../model/overlay';
import { resolveTerrain, type TerrainOptions } from '../model/terrain';
import { mowerAt, trailScene } from '../model/trail';
import { viewState, type View, type ViewState } from '../model/view';
import type { MapPanelOptions } from '../types';
import { MapView } from './MapView';
import { PanelMessage } from './PanelMessage';
import { ViewSwitch } from './ViewSwitch';

/** The wall clock, re-read every minute so a mower that stops reporting goes stale even with refresh off. */
const useNow = (): number => {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
};

/** The view the map is in. The model decides it (viewState); this remembers it between renders. */
const useView = (options: TerrainOptions | undefined): [View, (view: View) => void] => {
  const [remembered, remember] = useState<ViewState>();
  const state = viewState(options, remembered);
  // Kept as soon as the options start the panel over, so that an earlier switch cannot come back.
  if (state !== remembered) {
    remember(state);
  }
  return [state.view, (view) => remember({ ...state, view })];
};

export const MapPanel: React.FC<PanelProps<MapPanelOptions>> = ({ options, data, width, height }) => {
  const baseMap = useMemo(() => resolveBaseMap(options.baseMap), [options.baseMap]);
  const terrain = useMemo(() => resolveTerrain(options.terrain), [options.terrain]);
  const overlay = useMemo(() => resolveOverlay(options.overlay), [options.overlay]);
  const [view, setView] = useView(options.terrain);
  const { trailColumns, dockOrigin } = options;
  const trail = useMemo(
    () => trailScene(data.series, { trailColumns, dockOrigin }),
    [data.series, trailColumns, dockOrigin]
  );
  // Aged separately, so the minute tick restyles the marker without placing the Trail again.
  const now = useNow();
  const last = 'scene' in trail ? trail.scene.mower : undefined;
  const mower = useMemo(() => last && mowerAt(last, now), [last, now]);

  const message = (text: string) => <PanelMessage width={width} height={height} text={text} />;
  if ('problem' in baseMap) {
    return message(baseMap.problem);
  }
  if ('problem' in terrain) {
    return message(terrain.problem);
  }
  if ('problem' in overlay) {
    return message(overlay.problem);
  }
  if ('problem' in trail) {
    return message(trail.problem);
  }
  return (
    <MapView
      baseMap={baseMap.source}
      overlay={overlay.overlay}
      terrain={terrain.source}
      view={view}
      trail={trail.scene}
      mower={mower}
      width={width}
      height={height}
    >
      {terrain.source && <ViewSwitch view={view} onChange={setView} />}
    </MapView>
  );
};
