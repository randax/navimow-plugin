import { useMemo, useState } from 'react';
import type { DockOriginOptions } from '../model/dockOrigin';
import type { Ring } from '../model/drawing';
import { lawnText, parseLawn, type Lawn, type Zone } from '../model/lawn';

/** The Lawn being edited, with the text that mirrors it and what stops it from being saved. */
export interface LawnDraft {
  lawn: Lawn;
  dockOrigin: DockOriginOptions;
  zones: Zone[];
  text: string;
  /** Why the text cannot be read back, or why a Zone is not ready; Save waits for it to clear. */
  problem?: string;
  setDock: (change: Partial<DockOriginOptions>) => void;
  setZones: (zones: Zone[]) => void;
  setOutline: (outline: Ring | undefined) => void;
  setText: (text: string) => void;
}

/** A Zone with no identifier would not match any of the mower's, so it cannot be saved as it is. */
const zoneProblem = (zones: Zone[]): string | undefined => {
  const blank = zones.findIndex((z) => z.id.trim() === '');
  return blank < 0 ? undefined : `Zone ${blank + 1} needs the identifier the mower's app uses before saving.`;
};

/**
 * Every change from the map or the fields shows up in the text at once; a change typed into the
 * text is read back into the fields when it reads, and reported until it does.
 */
export function useLawnDraft(initial: Lawn): LawnDraft {
  const [lawn, setLawnOnly] = useState<Lawn>(initial);
  const [text, setTextOnly] = useState(() => lawnText(initial));
  const [textProblem, setTextProblem] = useState<string>();

  const setLawn = (next: Lawn) => {
    setLawnOnly(next);
    setTextOnly(lawnText(next));
    setTextProblem(undefined);
  };
  const setText = (value: string) => {
    setTextOnly(value);
    const parsed = parseLawn(value);
    if ('problem' in parsed) {
      setTextProblem(parsed.problem);
    } else {
      setTextProblem(undefined);
      setLawnOnly(parsed.lawn);
    }
  };

  const dockOrigin = useMemo(() => lawn.dockOrigin ?? {}, [lawn.dockOrigin]);
  const zones = lawn.boundary?.zones ?? [];
  return {
    lawn,
    dockOrigin,
    zones,
    text,
    problem: textProblem ?? zoneProblem(zones),
    setDock: (change) => setLawn({ ...lawn, dockOrigin: { ...dockOrigin, ...change } }),
    setZones: (next) => setLawn({ ...lawn, boundary: { ...lawn.boundary, zones: next } }),
    setOutline: (outline) => setLawn({ ...lawn, boundary: { ...lawn.boundary, outline } }),
    setText,
  };
}
