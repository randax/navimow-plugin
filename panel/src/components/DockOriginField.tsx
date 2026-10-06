import React, { useState } from 'react';
import type { StandardEditorProps } from '@grafana/data';
import { Input } from '@grafana/ui';
import type { DockOrigin } from '../model/dockOrigin';
import { enteredNumber, type Typed } from '../model/entry';
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
 * version moves all of its Lawn to where it is kept now. Grafana's own number field saves its one
 * value alone, which would leave the rest behind in the older place, where it is no longer read.
 */
export const DockOriginField: React.FC<
  StandardEditorProps<Lawn | undefined, DockOriginFieldSettings, MapPanelOptions>
> = ({ value, onChange, context, item, id }) => {
  const { field, min, max, placeholder } = item.settings!;
  // Once saved where it is kept now, `value` is the whole Lawn; until then it is wherever the panel holds it.
  const lawn = value ?? resolveLawn(context.options ?? {});
  const saved = lawn?.dockOrigin?.[field];
  // What is being typed, until the owner leaves the field: what to save of it is the model's decision.
  const [typed, setTyped] = useState<Typed>();
  const save = () => {
    const entered = enteredNumber(typed, saved);
    if (entered) {
      onChange(spelledOut(withDockOrigin(lawn, field, entered.number)));
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
      value={typed?.text ?? saved ?? ''}
      onChange={({ currentTarget }) =>
        setTyped({ text: currentTarget.value, unfinished: currentTarget.validity.badInput })
      }
      onBlur={save}
      onKeyDown={(event) => event.key === 'Enter' && save()}
    />
  );
};
