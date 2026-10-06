import React, { useState } from 'react';
import type { StandardEditorProps } from '@grafana/data';
import { Button } from '@grafana/ui';
import { resolveLawn, spelledOut, type Lawn } from '../model/lawn';
import type { MapPanelOptions } from '../types';
import { CalibrationDrawer } from './CalibrationDrawer';

/**
 * The option editor for the Lawn: a button in the options pane, and the drawer it opens. The
 * drawer is mounted only while open, so its map lives exactly as long as it is on screen.
 */
export const CalibrationEditor: React.FC<StandardEditorProps<Lawn | undefined, unknown, MapPanelOptions>> = ({
  value,
  onChange,
  context,
}) => {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button icon="map-marker" variant="secondary" fill="outline" onClick={() => setOpen(true)}>
        Calibrate on the map
      </Button>
      {open && (
        <CalibrationDrawer
          // Once saved where it is kept now, `value` is the whole Lawn; a panel saved by an earlier
          // version holds it elsewhere in the options.
          initial={value ?? resolveLawn(context.options ?? {}) ?? {}}
          options={context.options}
          data={context.data}
          onSave={(lawn) => {
            onChange(spelledOut(lawn));
            setOpen(false);
          }}
          onDiscard={() => setOpen(false)}
        />
      )}
    </>
  );
};
