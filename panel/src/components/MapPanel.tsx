import React, { useEffect, useMemo, useState } from 'react';
import { dateTimeFormat, type PanelProps } from '@grafana/data';
import { getTemplateSrv, locationService } from '@grafana/runtime';
import { resolveBaseMap } from '../model/baseMap';
import { coverageSettings, coverageState, type CoverageState } from '../model/coverage';
import { coverageScene } from '../model/coverageScene';
import { jobAt, trailDetail, zoneDetail, type DetailContext } from '../model/hover';
import { boundaryFeatures, resolveLawn } from '../model/lawn';
import { resolveOverlay } from '../model/overlay';
import { jobVariableName, selectedJobs } from '../model/selection';
import type { Hideable } from '../model/style';
import { resolveTerrain } from '../model/terrain';
import { mowerAt, trailScene } from '../model/trail';
import { followState, viewState, type FollowState, type ViewState } from '../model/view';
import { readZoneProgress } from '../model/zoneProgress';
import type { MapPanelOptions } from '../types';
import { CoverageControl } from './CoverageControl';
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

/**
 * What a switch on the panel is set to. The model decides it from what it was before (`next`), and
 * hands back the same state for as long as nothing starts the panel over; this remembers it between
 * renders, as soon as it changes, so that an earlier switch cannot come back.
 */
const useSwitch = <S,>(next: (previous?: S) => S): [S, (state: S) => void] => {
  const [remembered, remember] = useState<S>();
  const state = next(remembered);
  if (state !== remembered) {
    remember(state);
  }
  return [state, remember];
};

export const MapPanel: React.FC<PanelProps<MapPanelOptions>> = ({ options, data, width, height, timeZone }) => {
  const baseMap = useMemo(() => resolveBaseMap(options.baseMap), [options.baseMap]);
  const terrain = useMemo(() => resolveTerrain(options.terrain), [options.terrain]);
  const overlay = useMemo(() => resolveOverlay(options.overlay), [options.overlay]);
  const [viewed, setViewed] = useSwitch<ViewState>((previous) => viewState(options.terrain, previous));
  const { view } = viewed;
  const [followed, setFollowed] = useSwitch<FollowState>((previous) => followState(options.follow, previous));
  const { following } = followed;
  const [hidden, setHidden] = useState<Hideable[]>([]);
  const [covered, setCovered] = useSwitch<CoverageState>((previous) => coverageState(options.coverage, previous));
  const covering = useByValue(coverageSettings(options.coverage, covered));
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
  // From the Trails as drawn, so Coverage narrows to the selected Job with them.
  const placed = 'scene' in trail ? trail.scene : undefined;
  const coverage = useMemo(
    () => (placed?.trails && placed.origin ? coverageScene(placed.trails, placed.origin, covering) : undefined),
    [placed, covering]
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
  const { scene } = trail;
  const context: DetailContext = {
    formatTime: (time) => dateTimeFormat(time, { timeZone }),
    zones: lawn?.boundary?.zones,
    progress,
  };
  // A click on a Trail selects its Job, where the dashboard has a variable to hold one. The variable
  // is the dashboard's: set through the address, as its own picker sets it.
  const selectionAt = (hit: MapHit) => {
    const job = jobs && hit.on === 'trail' ? jobAt(scene, hit.trail) : undefined;
    return job === undefined ? undefined : () => locationService.partial({ [`var-${jobVariable}`]: job }, true);
  };
  return (
    <MapView
      baseMap={baseMap.source}
      overlay={overlay.overlay}
      terrain={terrain.source}
      view={view}
      trail={scene}
      boundary={boundary}
      coverage={coverage}
      raised={covering.raised}
      mower={mower}
      width={width}
      height={height}
      hidden={hidden}
      onHidden={setHidden}
      following={following}
      onFollow={(on) => setFollowed({ ...followed, following: on })}
      detailOf={(hit) =>
        hit.on === 'trail' ? trailDetail(scene, hit.trail, hit.at, context) : zoneDetail(hit.zone, context)
      }
      selectionAt={selectionAt}
      controls={
        <CoverageControl
          style={covered.style}
          raised={covered.raised}
          legend={coverage?.legend}
          onChange={(to) => setCovered({ ...covered, ...to })}
        />
      }
    >
      {terrain.source && <ViewSwitch view={view} onChange={(to) => setViewed({ ...viewed, view: to })} />}
      {trail.warning && <MapWarning text={trail.warning} />}
      {coverage?.note && !hidden.includes('coverage') && <MapWarning text={coverage.note} />}
    </MapView>
  );
};
