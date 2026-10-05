import React, { useEffect, useMemo, useState } from 'react';
import { dateTimeFormat, type PanelProps } from '@grafana/data';
import { getTemplateSrv, locationService } from '@grafana/runtime';
import { resolveBaseMap } from '../model/baseMap';
import { jobAt, trailDetail, zoneDetail, type DetailContext } from '../model/hover';
import { boundaryFeatures, resolveLawn } from '../model/lawn';
import { resolveOverlay } from '../model/overlay';
import { jobVariableName, selectedJobs } from '../model/selection';
import type { Layer } from '../model/style';
import { resolveTerrain, type TerrainOptions } from '../model/terrain';
import { mowerAt, trailScene } from '../model/trail';
import { followState, viewState, type FollowState, type View, type ViewState } from '../model/view';
import { readZoneProgress } from '../model/zoneProgress';
import type { MapPanelOptions } from '../types';
import { MapView, useByValue, type MapHit } from './MapView';
import { MapWarning } from './MapWarning';
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
const useView = (options: TerrainOptions | null | undefined): [View, (view: View) => void] => {
  const [remembered, remember] = useState<ViewState>();
  const state = viewState(options, remembered);
  // Kept as soon as the options start the panel over, so that an earlier switch cannot come back.
  if (state !== remembered) {
    remember(state);
  }
  return [state.view, (view) => remember({ ...state, view })];
};

/** Whether the view follows the mower. The model decides it (followState); this remembers it. */
const useFollow = (option: boolean | undefined): [boolean, (following: boolean) => void] => {
  const [remembered, remember] = useState<FollowState>();
  const state = followState(option, remembered);
  if (state !== remembered) {
    remember(state);
  }
  return [state.following, (following) => remember({ ...state, following })];
};

export const MapPanel: React.FC<PanelProps<MapPanelOptions>> = ({ options, data, width, height, timeZone }) => {
  const baseMap = useMemo(() => resolveBaseMap(options.baseMap), [options.baseMap]);
  const terrain = useMemo(() => resolveTerrain(options.terrain), [options.terrain]);
  const overlay = useMemo(() => resolveOverlay(options.overlay), [options.overlay]);
  const [view, setView] = useView(options.terrain);
  const [following, setFollowing] = useFollow(options.follow);
  const [hidden, setHidden] = useState<Layer[]>([]);
  const { trailColumns, zoneProgressColumns, lawns, lawn: single, dockOrigin: atRoot } = options;
  // Keyed on the places the Lawn may be saved, so a fresh options object alone changes nothing.
  const lawn = useMemo(() => resolveLawn({ lawns, lawn: single, dockOrigin: atRoot }), [lawns, single, atRoot]);
  const dockOrigin = lawn?.dockOrigin;
  const jobVariable = jobVariableName(options.jobVariable);
  // Undefined on a dashboard without the variable, where there is nothing for a click to set.
  const jobs = useByValue(
    selectedJobs(
      getTemplateSrv()
        .getVariables()
        .find((v) => v.name === jobVariable)
    )
  );
  const trail = useMemo(
    () => trailScene(data.series, { trailColumns, dockOrigin, jobs }),
    [data.series, trailColumns, dockOrigin, jobs]
  );
  const progress = useMemo(
    () => readZoneProgress(data.series, zoneProgressColumns),
    [data.series, zoneProgressColumns]
  );
  const boundary = useMemo(() => boundaryFeatures(lawn?.boundary, progress), [lawn?.boundary, progress]);
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
  const { scene } = trail;
  const context: DetailContext = {
    formatTime: (time) => dateTimeFormat(time, { timeZone }),
    zones: lawn?.boundary?.zones,
    progress,
  };
  const jobOf = (hit: MapHit) => (jobs && hit.layer === 'trail' ? jobAt(scene, hit.trail) : undefined);
  return (
    <MapView
      baseMap={baseMap.source}
      overlay={overlay.overlay}
      terrain={terrain.source}
      view={view}
      trail={scene}
      boundary={boundary}
      mower={mower}
      width={width}
      height={height}
      hidden={hidden}
      onHidden={setHidden}
      following={following}
      onFollow={setFollowing}
      detailOf={(hit) =>
        hit.layer === 'trail' ? trailDetail(scene, hit.trail, hit.at, context) : zoneDetail(hit.zone, context)
      }
      selects={(hit) => jobOf(hit) !== undefined}
      onSelect={(hit) => {
        const job = jobOf(hit);
        if (job !== undefined) {
          // The variable is the dashboard's: set through the address, as its own picker sets it.
          locationService.partial({ [`var-${jobVariable}`]: job }, true);
        }
      }}
    >
      {terrain.source && <ViewSwitch view={view} onChange={setView} />}
      {trail.warning && <MapWarning text={trail.warning} />}
    </MapView>
  );
};
