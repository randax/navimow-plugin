# Prototype: Dock origin calibration and Boundary drawing UX (throwaway)

Wayfinder ticket #16. Open `index.html` in a browser (double-click; needs internet for Kartverket tiles and the MapLibre CDN). Switch variants with the yellow bar, the arrow keys, or `?variant=A|B|C`.

Question: how should a user place the dock, rotate the Trail onto the lawn, and draw the lawn and Zones, inside a Grafana Drawer?

- **A. Sidebar: drag + slider.** Map left, form right. Drag the dock marker or type coordinates; rotation slider with number input; Draw lawn / Add zone buttons; editable zone list with ids; saved-options JSON visible.
- **B. Direct manipulation, no sidebar.** Full-bleed map. Drag the dock, drag the white handle at the end of the mower's x-axis to rotate; floating toolbar for drawing; one-line readout; undo last shape.
- **C. Guided wizard.** Three steps: click to place dock, nudge rotation by 1°/10° or enter a compass bearing, then draw lawn and zones with ids.

All variants share the same model: dock lat/lng, rotation of the mower x-axis, lawn ring, zones with id + name. Drawing is click-to-add, double-click-to-close in every variant.
