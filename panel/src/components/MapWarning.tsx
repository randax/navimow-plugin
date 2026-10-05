import React from 'react';
import { css } from '@emotion/css';
import type { GrafanaTheme2 } from '@grafana/data';
import { Icon, useStyles2 } from '@grafana/ui';

/** Something the owner should know about a map that is drawn all the same. */
export const MapWarning: React.FC<{ text: string }> = ({ text }) => {
  const styles = useStyles2(getStyles);
  return (
    <div className={styles.warning} role="status" data-testid="navimow-map-warning">
      <Icon name="exclamation-triangle" className={styles.icon} />
      <span>{text}</span>
    </div>
  );
};

const getStyles = (theme: GrafanaTheme2) => ({
  warning: css({
    display: 'flex',
    gap: theme.spacing(1),
    padding: theme.spacing(0.5, 1),
    background: theme.colors.background.primary,
    borderLeft: `3px solid ${theme.colors.warning.border}`,
    borderRadius: theme.shape.radius.default,
    boxShadow: theme.shadows.z1,
    fontSize: theme.typography.bodySmall.fontSize,
    lineHeight: theme.typography.bodySmall.lineHeight,
  }),
  icon: css({ flexShrink: 0, marginTop: 2, color: theme.colors.warning.text }),
});
