import React from 'react';
import { css } from '@emotion/css';
import type { GrafanaTheme2 } from '@grafana/data';
import { Checkbox, RadioButtonGroup, Toggletip, ToolbarButton, useStyles2 } from '@grafana/ui';
import { COVERAGE_STYLES, type CoverageLegend, type CoverageStyle } from '../model/coverage';

interface Props {
  style: CoverageStyle;
  raised: boolean;
  /** What the colours on the map mean. Absent while there is no Coverage to draw. */
  legend?: CoverageLegend;
  onChange: (to: { style: CoverageStyle; raised: boolean }) => void;
}

/**
 * The on-panel control that switches how Coverage is drawn: its style, and flat or raised. It
 * changes the view and nothing that is saved. The key to the colours is kept here, off the map.
 */
export const CoverageControl: React.FC<Props> = ({ style, raised, legend, onChange }) => {
  const styles = useStyles2(getStyles);
  return (
    <Toggletip
      placement="left-start"
      closeButton={false}
      fitContent
      content={
        <div className={styles.content} data-testid="navimow-map-coverage">
          <RadioButtonGroup
            size="sm"
            options={COVERAGE_STYLES}
            value={style}
            onChange={(to) => onChange({ style: to, raised })}
            aria-label="Coverage style"
          />
          <Checkbox label="Raised" value={raised} onChange={() => onChange({ style, raised: !raised })} />
          {legend && (
            <div className={styles.legend}>
              <span>{legend.title}</span>
              <div
                className={styles.ramp}
                style={{ background: `linear-gradient(90deg, ${legend.colours.join(', ')})` }}
              />
              <div className={styles.ends}>
                <span>{legend.from}</span>
                <span>{legend.to}</span>
              </div>
            </div>
          )}
        </div>
      }
    >
      <ToolbarButton variant="canvas" icon="gf-grid" aria-label="Coverage" disabled={!legend} />
    </Toggletip>
  );
};

const getStyles = (theme: GrafanaTheme2) => ({
  content: css({ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: theme.spacing(1.5) }),
  legend: css({ alignSelf: 'stretch', fontSize: theme.typography.bodySmall.fontSize }),
  ramp: css({ height: theme.spacing(1), margin: theme.spacing(0.5, 0), borderRadius: theme.shape.radius.default }),
  ends: css({ display: 'flex', justifyContent: 'space-between', color: theme.colors.text.secondary }),
});
