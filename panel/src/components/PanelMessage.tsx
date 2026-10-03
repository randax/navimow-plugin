import React from 'react';
import { css } from '@emotion/css';
import type { GrafanaTheme2 } from '@grafana/data';
import { useStyles2 } from '@grafana/ui';

/** Shown in place of the map when it cannot be drawn, so the panel is never silently blank. */
export const PanelMessage: React.FC<{ width: number; height: number; text: string }> = ({ width, height, text }) => {
  const styles = useStyles2(getStyles);
  return (
    <div className={styles.message} style={{ width, height }} data-testid="navimow-map-message">
      {text}
    </div>
  );
};

const getStyles = (theme: GrafanaTheme2) => ({
  message: css({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: theme.spacing(2),
    textAlign: 'center',
    color: theme.colors.text.secondary,
  }),
});
