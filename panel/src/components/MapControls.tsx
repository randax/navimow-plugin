import React, { type ReactNode } from 'react';
import { css } from '@emotion/css';
import type { GrafanaTheme2 } from '@grafana/data';
import { Checkbox, Toggletip, ToolbarButton, Tooltip, useStyles2 } from '@grafana/ui';
import type { Hideable } from '../model/style';

interface Props {
  /** Where the top of the map points, in degrees clockwise from north. */
  bearing: number;
  following: boolean;
  /** What the map has something to draw for, and so offers to hide. */
  hideable: ReadonlyArray<{ id: Hideable; label: string }>;
  hidden: readonly Hideable[];
  /** Nothing to fit to, or to follow, until a Trail is on the map. */
  canFit: boolean;
  canFollow: boolean;
  onZoom: (by: 1 | -1) => void;
  onNorth: () => void;
  onFit: () => void;
  onFollow: (following: boolean) => void;
  onHidden: (hidden: Hideable[]) => void;
  /** More controls for the foot of the stack, made by whoever owns what they switch. */
  children?: ReactNode;
}

// A needle with its north half coloured, turned against the map so that it keeps pointing north.
const Needle: React.FC<{ bearing: number }> = ({ bearing }) => (
  <svg viewBox="0 0 16 16" width="16" height="16" style={{ transform: `rotate(${-bearing}deg)` }} aria-hidden="true">
    <path d="M8 1 11.5 8H4.5Z" fill="#E02F44" />
    <path d="M8 15 4.5 8H11.5Z" fill="currentColor" opacity="0.7" />
  </svg>
);

/**
 * The controls laid over the map: all of them steer the view and none change what is saved. What
 * they do to the map is the caller's; this is only the buttons.
 */
export const MapControls: React.FC<Props> = ({
  bearing,
  following,
  hideable,
  hidden,
  canFit,
  canFollow,
  onZoom,
  onNorth,
  onFit,
  onFollow,
  onHidden,
  children,
}) => {
  const styles = useStyles2(getStyles);
  const toggle = (id: Hideable) => onHidden(hidden.includes(id) ? hidden.filter((h) => h !== id) : [...hidden, id]);
  return (
    <div className={styles.controls} data-testid="navimow-map-controls">
      <Control name="Zoom in" icon="plus" onClick={() => onZoom(1)} />
      <Control name="Zoom out" icon="minus" onClick={() => onZoom(-1)} />
      <Control name="Turn north up" icon={<Needle bearing={bearing} />} onClick={onNorth} />
      <Control name="Fit to Trail" icon="capture" disabled={!canFit} onClick={onFit} />
      <Control
        name="Follow the mower"
        icon="crosshair"
        variant={following ? 'active' : 'canvas'}
        aria-pressed={following}
        disabled={!canFollow}
        onClick={() => onFollow(!following)}
      />
      <Toggletip
        placement="left-start"
        closeButton={false}
        fitContent
        content={
          <div className={styles.list}>
            {hideable.map(({ id, label }) => (
              <Checkbox key={id} label={label} value={!hidden.includes(id)} onChange={() => toggle(id)} />
            ))}
          </div>
        }
      >
        <ToolbarButton variant="canvas" icon="layer-group" aria-label="Show or hide" />
      </Toggletip>
      {children}
    </div>
  );
};

/**
 * One button of the stack. Its name is told to the side: the toolbar button's own tooltip opens
 * underneath, over the next button down.
 */
const Control: React.FC<Omit<React.ComponentProps<typeof ToolbarButton>, 'aria-label'> & { name: string }> = ({
  name,
  ...button
}) => (
  <Tooltip content={name} placement="left">
    <ToolbarButton variant="canvas" {...button} aria-label={name} />
  </Tooltip>
);

const getStyles = (theme: GrafanaTheme2) => ({
  controls: css({
    position: 'absolute',
    top: theme.spacing(1),
    right: theme.spacing(1),
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(0.5),
    // The buttons are made for a toolbar's background; over a map each needs one of its own.
    button: { background: theme.colors.background.primary, boxShadow: theme.shadows.z1 },
  }),
  list: css({ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: theme.spacing(1) }),
});
