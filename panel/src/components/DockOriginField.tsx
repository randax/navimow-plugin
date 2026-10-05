import React, { useState } from 'react';
import type { StandardEditorProps } from '@grafana/data';
import { Input } from '@grafana/ui';
import type { DockOrigin } from '../model/dockOrigin';
import { resolveLawn, spelledOut, withDockOrigin, type Lawn } from '../model/lawn';
import type { MapPanelOptions } from '../types';

export interface DockOriginFieldSettings {
  field: keyof DockOrigin;
  min?: number;
  max?: number;
  placeholder?: string;
}

/**
 * One value of the Dock origin as a plain number field. It reads the Lawn wherever the panel holds
 * it and saves the whole Lawn back, so that typing one value into a panel saved by an earlier
 * version moves all of its Lawn to where it is kept now, and leaves none of it behind. Grafana's
 * own number field saves its one value alone, which would.
 */
export const DockOriginField: React.FC<
  StandardEditorProps<Lawn | undefined, DockOriginFieldSettings, MapPanelOptions>
> = ({ value, onChange, context, item, id }) => {
  const { field, min, max, placeholder } = item.settings!;
  // Once saved where it is kept now, `value` is the whole Lawn; until then it is wherever the panel holds it.
  const lawn = value ?? resolveLawn(context.options ?? {});
  const saved = lawn?.dockOrigin?.[field];
  // What is being typed, until it is done: half a number, such as "-" or "59.", is not one to save.
  const [typed, setTyped] = useState<string>();
  const save = () => {
    const number = typed?.trim() === '' ? undefined : Number(typed);
    if (typed !== undefined && number !== saved && !Number.isNaN(number)) {
      onChange(spelledOut(withDockOrigin(lawn, field, number)));
    }
    setTyped(undefined);
  };
  return (
    <Input
      id={id}
      type="number"
      step="any"
      min={min}
      max={max}
      placeholder={placeholder}
      value={typed ?? saved ?? ''}
      onChange={(event) => setTyped(event.currentTarget.value)}
      onBlur={save}
      onKeyDown={(event) => event.key === 'Enter' && save()}
    />
  );
};
