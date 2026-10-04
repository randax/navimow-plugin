import React from 'react';
import { css } from '@emotion/css';
import type { GrafanaTheme2 } from '@grafana/data';
import { RadioButtonGroup, useStyles2 } from '@grafana/ui';
import type { View } from '../model/terrain';

export const VIEWS: Array<{ value: View; label: string }> = [
  { value: 'flat', label: 'Flat' },
  { value: 'terrain', label: 'Terrain' },
];

/** The on-panel control that switches the map between flat and terrain. */
export const ViewSwitch: React.FC<{ view: View; onChange: (view: View) => void }> = ({ view, onChange }) => {
  const styles = useStyles2(getStyles);
  return (
    <div className={styles.control} data-testid="navimow-map-view">
      <RadioButtonGroup size="sm" options={VIEWS} value={view} onChange={onChange} aria-label="Map view" />
    </div>
  );
};

const getStyles = (theme: GrafanaTheme2) => ({
  control: css({
    position: 'absolute',
    top: theme.spacing(1),
    left: theme.spacing(1),
    // The group itself is see-through, made for a panel's background rather than a map's.
    background: theme.colors.background.primary,
    borderRadius: theme.shape.radius.default,
    boxShadow: theme.shadows.z1,
  }),
});
