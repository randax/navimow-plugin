# Changelog

## Unreleased

- Base map picker: Kartverket topo, gråtone and turkart, OpenStreetMap, and a custom tile or WMS source with required attribution.
- Terrain: Mapterhorn, AWS Terrain Tiles or custom elevation tiles, with a switch on the panel between flat and terrain.
- Overlay: Kartverket hillshade or a custom tile or WMS source, drawn between the Base map and the Trail with an opacity.
- Hovering a Trail shows its time, Job, Zone and status; hovering a Zone shows its name and latest progress.
- Clicking a Trail sets a dashboard Job variable, and the map narrows to the Jobs that variable holds.
- An optional Zone progress query colours the traced Zones.
- Controls on the panel: zoom, north up, fit to Trail, follow the mower, and visibility of the Trail and the Boundary.
- A warning when a panel receives positions from more than one mower.
- The Dock origin and Boundary are saved under a key per mower, so a panel for several mowers can come later without moving what is saved. A panel for one mower uses the key for any mower; panels saved earlier are read as before and moved on their next save.
