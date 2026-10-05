import React from 'react';
import { Portal, VizTooltipContainer, VizTooltipContent, VizTooltipHeader, VizTooltipWrapper } from '@grafana/ui';
import type { Detail } from '../model/hover';

/**
 * What the map tells about the thing under the pointer, in the tooltip every Grafana panel uses.
 * `at` is where the pointer is in the window; the tooltip is drawn outside the panel so that it is
 * neither clipped by it nor placed by the dashboard's own positioning of its panels.
 */
export const MapTooltip: React.FC<{ at: { x: number; y: number }; detail: Detail }> = ({ at, detail }) => (
  <Portal>
    <VizTooltipContainer position={at} offset={{ x: 12, y: 12 }}>
      <VizTooltipWrapper>
        <div data-testid="navimow-map-tooltip">
          <VizTooltipHeader item={{ label: '', value: detail.title, color: detail.colour }} />
          {detail.rows.length > 0 && <VizTooltipContent items={detail.rows} />}
        </div>
      </VizTooltipWrapper>
    </VizTooltipContainer>
  </Portal>
);
