import React, { useMemo, useState } from 'react';
import { css } from '@emotion/css';
import type { DataFrame, GrafanaTheme2 } from '@grafana/data';
import { Button, Drawer, Field, IconButton, Input, Slider, Stack, Text, TextArea, useStyles2 } from '@grafana/ui';
import { resolveBaseMap } from '../model/baseMap';
import { resolveDockOrigin } from '../model/dockOrigin';
import { closeDraft, EMPTY_DRAFT, placeVertex, type Click, type Draft, type Ring } from '../model/drawing';
import { boundaryFeatures, lawnText, nextZoneId, parseLawn, type Lawn, type Zone } from '../model/lawn';
import { resolveOverlay } from '../model/overlay';
import { EMPTY_SCENE, trailScene } from '../model/trail';
import type { MapPanelOptions } from '../types';
import { CalibrationMap } from './CalibrationMap';
import { PanelMessage } from './PanelMessage';

interface Props {
  initial: Lawn;
  options?: MapPanelOptions;
  data: DataFrame[];
  onSave: (lawn: Lawn) => void;
  onDiscard: () => void;
}

/** Which polygon is being drawn: the lawn's outline, a new Zone, or a Zone drawn again. */
type Drawing = { kind: 'outline' } | { kind: 'zone'; index?: number };

// Coordinates to a centimetre, rotation to a tenth of a degree: what a drag can mean, and no
// more digits for the owner to read.
const round = (value: number, decimals: number) => Number(value.toFixed(decimals));

const styles = (theme: GrafanaTheme2) => ({
  layout: css({ display: 'flex', gap: theme.spacing(2), height: '100%', minHeight: 480 }),
  map: css({ flex: 1, minWidth: 0, position: 'relative' }),
  sidebar: css({
    width: 360,
    flexShrink: 0,
    overflowY: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(2),
    paddingRight: theme.spacing(1),
  }),
  pair: css({ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: theme.spacing(1) }),
  zone: css({ display: 'grid', gridTemplateColumns: '5em 1fr auto auto', gap: theme.spacing(0.5), alignItems: 'center' }),
  text: css({ fontFamily: theme.typography.fontFamilyMonospace, fontSize: theme.typography.bodySmall.fontSize }),
  footer: css({ marginTop: 'auto', paddingTop: theme.spacing(1) }),
});

/**
 * The drawer in which the owner calibrates: a map to drag the dock and turn the Trail on, and a
 * sidebar carrying the same values as fields, the Boundary's Zones, and the option as text. Nothing
 * leaves the drawer until Save; Discard forgets it all.
 */
export const CalibrationDrawer: React.FC<Props> = ({ initial, options, data, onSave, onDiscard }) => {
  const s = useStyles2(styles);
  const [lawn, setLawnOnly] = useState<Lawn>(initial);
  const [text, setText] = useState(() => lawnText(initial));
  const [textProblem, setTextProblem] = useState<string>();
  const [drawing, setDrawing] = useState<Drawing>();
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [drawProblem, setDrawProblem] = useState<string>();

  // Every change from the map or the fields shows up in the text at once; a change typed into the
  // text is read back into the fields, when it reads.
  const setLawn = (next: Lawn) => {
    setLawnOnly(next);
    setText(lawnText(next));
    setTextProblem(undefined);
  };
  const onText = (value: string) => {
    setText(value);
    const parsed = parseLawn(value);
    if ('problem' in parsed) {
      setTextProblem(parsed.problem);
    } else {
      setTextProblem(undefined);
      setLawnOnly(parsed.lawn);
    }
  };

  const dockOrigin = useMemo(() => lawn.dockOrigin ?? {}, [lawn.dockOrigin]);
  const setDock = (change: Partial<typeof dockOrigin>) => setLawn({ ...lawn, dockOrigin: { ...dockOrigin, ...change } });
  const zones = lawn.boundary?.zones ?? [];
  const setZones = (next: Zone[]) => setLawn({ ...lawn, boundary: { ...lawn.boundary, zones: next } });
  const setOutline = (outline: Ring | undefined) => setLawn({ ...lawn, boundary: { ...lawn.boundary, outline } });

  const resolved = resolveDockOrigin(dockOrigin);
  const origin = 'origin' in resolved ? resolved.origin : undefined;
  const baseMap = useMemo(() => resolveBaseMap(options?.baseMap), [options?.baseMap]);
  const overlay = useMemo(() => resolveOverlay(options?.overlay), [options?.overlay]);
  const trail = useMemo(() => {
    const scene = trailScene(data, { trailColumns: options?.trailColumns, dockOrigin });
    return 'scene' in scene ? scene.scene : EMPTY_SCENE;
  }, [data, options?.trailColumns, dockOrigin]);
  const boundary = useMemo(() => boundaryFeatures(lawn.boundary), [lawn.boundary]);

  const start = (next: Drawing) => {
    setDrawing(next);
    setDraft(EMPTY_DRAFT);
    setDrawProblem(undefined);
  };
  const stop = () => {
    setDrawing(undefined);
    setDraft(EMPTY_DRAFT);
  };
  const finished = (ring: Ring) => {
    if (drawing?.kind === 'outline') {
      setOutline(ring);
    } else if (drawing?.index === undefined) {
      const id = nextZoneId(zones);
      setZones([...zones, { id, name: `Zone ${id}`, ring }]);
    } else {
      setZones(zones.map((z, i) => (i === drawing.index ? { ...z, ring } : z)));
    }
    stop();
  };
  const step = (result: ReturnType<typeof placeVertex>) => {
    if ('draft' in result) {
      setDraft(result.draft);
    } else if ('ring' in result) {
      finished(result.ring);
    } else {
      setDrawProblem(result.problem);
    }
  };
  const onClick = (click: Click) => {
    if (drawing) {
      step(placeVertex(draft, click));
    } else if (!origin) {
      // The first click places a dock that has no position yet; after that it is dragged.
      setDock({ lat: round(click.lngLat[1], 7), lon: round(click.lngLat[0], 7), rotation: dockOrigin.rotation ?? 0 });
    }
  };

  const number = (value: number | undefined) => (value === undefined ? '' : String(value));
  const onNumber = (key: 'lat' | 'lon' | 'rotation') => (event: React.ChangeEvent<HTMLInputElement>) =>
    setDock({ [key]: event.target.value === '' ? undefined : event.target.valueAsNumber });

  return (
    <Drawer title="Dock origin and Boundary" size="lg" onClose={onDiscard} closeOnMaskClick={false}>
      <div className={s.layout}>
        <div className={s.map}>
          {'problem' in baseMap ? (
            <PanelMessage width={600} height={480} text={baseMap.problem} />
          ) : (
            <CalibrationMap
              baseMap={baseMap.source}
              overlay={'overlay' in overlay ? overlay.overlay : undefined}
              origin={origin}
              trail={trail}
              boundary={boundary}
              draft={draft}
              drawing={drawing !== undefined}
              onDock={(lat, lon) => setDock({ lat: round(lat, 7), lon: round(lon, 7) })}
              onRotation={(rotation) => setDock({ rotation: round(rotation, 1) })}
              onClick={onClick}
              onDoubleClick={() => step(closeDraft(draft))}
            />
          )}
        </div>
        <div className={s.sidebar}>
          <Text element="p" color="secondary">
            {origin
              ? 'Drag the dock onto its place on the map. Drag the handle at the end of the axis until the Trail lies along the lawn.'
              : 'Click the map where the charging dock stands.'}
          </Text>
          <div className={s.pair}>
            <Field label="Latitude" description="Of the charging dock">
              <Input type="number" step="any" min={-85} max={85} value={number(dockOrigin.lat)} onChange={onNumber('lat')} />
            </Field>
            <Field label="Longitude" description="Of the charging dock">
              <Input type="number" step="any" min={-180} max={180} value={number(dockOrigin.lon)} onChange={onNumber('lon')} />
            </Field>
          </div>
          <Field label="Rotation" description="Bearing of the mower's x-axis, in degrees clockwise from north">
            <Stack direction="row" alignItems="center" gap={1}>
              <div style={{ flex: 1 }}>
                <Slider
                  min={0}
                  max={360}
                  step={1}
                  value={dockOrigin.rotation ?? 0}
                  onChange={(rotation) => setDock({ rotation })}
                />
              </div>
              <Input
                type="number"
                step="any"
                width={10}
                aria-label="Rotation in degrees"
                value={number(dockOrigin.rotation)}
                onChange={onNumber('rotation')}
              />
            </Stack>
          </Field>

          <Field label="Boundary" description="Click each corner on the map; double-click the last one to close.">
            <Stack direction="column" gap={1}>
              <Stack direction="row" alignItems="center" gap={1}>
                <Text>{lawn.boundary?.outline ? `Lawn: ${lawn.boundary.outline.length} corners` : 'Lawn: not drawn'}</Text>
                <Button size="sm" variant="secondary" disabled={!origin || !!drawing} onClick={() => start({ kind: 'outline' })}>
                  {lawn.boundary?.outline ? 'Redraw lawn' : 'Draw lawn'}
                </Button>
                {lawn.boundary?.outline && (
                  <IconButton name="trash-alt" tooltip="Remove the lawn outline" onClick={() => setOutline(undefined)} />
                )}
              </Stack>
              {zones.map((zone, i) => (
                <div className={s.zone} key={i}>
                  <Input
                    aria-label={`Zone ${i + 1} identifier`}
                    placeholder="id"
                    value={zone.id}
                    onChange={(e) => setZones(zones.map((z, j) => (j === i ? { ...z, id: e.currentTarget.value } : z)))}
                  />
                  <Input
                    aria-label={`Zone ${i + 1} name`}
                    placeholder="Name"
                    value={zone.name}
                    onChange={(e) => setZones(zones.map((z, j) => (j === i ? { ...z, name: e.currentTarget.value } : z)))}
                  />
                  <IconButton
                    name="pen"
                    tooltip="Draw this Zone again"
                    disabled={!!drawing}
                    onClick={() => start({ kind: 'zone', index: i })}
                  />
                  <IconButton
                    name="trash-alt"
                    tooltip="Remove this Zone"
                    onClick={() => setZones(zones.filter((_, j) => j !== i))}
                  />
                </div>
              ))}
              <Stack direction="row" alignItems="center" gap={1}>
                <Button size="sm" variant="secondary" disabled={!origin || !!drawing} onClick={() => start({ kind: 'zone' })}>
                  Add Zone
                </Button>
                {drawing && (
                  <>
                    <Text color="secondary">
                      {draft.vertices.length === 0 ? 'Click the first corner' : `${draft.vertices.length} corners so far`}
                    </Text>
                    <Button size="sm" variant="secondary" fill="text" onClick={stop}>
                      Cancel
                    </Button>
                  </>
                )}
              </Stack>
              {drawProblem && <Text color="error">{drawProblem}</Text>}
              {!origin && <Text color="secondary">Place the dock first, so the Boundary has somewhere to be.</Text>}
              <Text color="secondary" variant="bodySmall">
                Each Zone&apos;s identifier is the one the mower&apos;s own app uses, so Zone progress colours the right grass.
              </Text>
            </Stack>
          </Field>

          <Field
            label="Saved as"
            description="What Save writes into the panel options. Paste a Lawn here to load it."
            invalid={textProblem !== undefined}
            error={textProblem}
          >
            <Stack direction="column" gap={0.5}>
              <TextArea className={s.text} rows={10} value={text} onChange={(e) => onText(e.currentTarget.value)} />
              <div>
                <Button
                  size="sm"
                  variant="secondary"
                  fill="text"
                  icon="copy"
                  onClick={() => navigator.clipboard?.writeText(text)}
                >
                  Copy
                </Button>
              </div>
            </Stack>
          </Field>

          <div className={s.footer}>
            <Stack direction="row" justifyContent="flex-end" gap={1}>
              <Button variant="secondary" onClick={onDiscard}>
                Discard
              </Button>
              <Button variant="primary" disabled={textProblem !== undefined} onClick={() => onSave(lawn)}>
                Save
              </Button>
            </Stack>
          </div>
        </div>
      </div>
    </Drawer>
  );
};
