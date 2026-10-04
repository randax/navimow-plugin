import React, { useEffect, useMemo, useState } from 'react';
import type { PanelProps } from '@grafana/data';
import { resolveSources } from '../model/style';
import type { View } from '../model/terrain';
import { mowerAt, trailScene } from '../model/trail';
import { initialView } from '../model/view';
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

/** The view the map is in: the one the options start in, until the owner switches on the panel. */
const useView = (start: View): [View, (view: View) => void] => {
  // A switch holds only while the options still start where it was made from, so editing them shows.
  const [switched, setSwitched] = useState<{ from: View; to: View }>();
  return [switched?.from === start ? switched.to : start, (to) => setSwitched({ from: start, to })];
};

export const MapPanel: React.FC<PanelProps<MapPanelOptions>> = ({ options, data, width, height }) => {
  const { baseMap, terrain, overlay, trailColumns, dockOrigin } = options;
  const resolved = useMemo(() => resolveSources({ baseMap, terrain, overlay }), [baseMap, terrain, overlay]);
  const [view, setView] = useView(initialView(terrain));
  const trail = useMemo(
    () => trailScene(data.series, { trailColumns, dockOrigin }),
    [data.series, trailColumns, dockOrigin]
  );
  // Aged separately, so the minute tick restyles the marker without placing the Trail again.
  const now = useNow();
  const last = 'scene' in trail ? trail.scene.mower : undefined;
  const mower = useMemo(() => last && mowerAt(last, now), [last, now]);

  if ('problem' in resolved) {
    return <PanelMessage width={width} height={height} text={resolved.problem} />;
  }
  if ('problem' in trail) {
    return <PanelMessage width={width} height={height} text={trail.problem} />;
  }
  const { sources } = resolved;
  return (
    <MapView
      {...sources}
      // Flat is the map without its Terrain; the switch recreates it with or without.
      terrain={view === 'terrain' ? sources.terrain : undefined}
      trail={trail.scene}
      mower={mower}
      width={width}
      height={height}
    >
      {sources.terrain && <ViewSwitch view={view} onChange={setView} />}
    </MapView>
  );
};
