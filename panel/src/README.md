# Navimow Map

Draws a Navimow robotic mower's Trail on a real map of the lawn.

## Base map

Choose a Base map under **Base map** in the panel options:

- **Kartverket topo** (default), **topo gråtone** and **turkart (toporaster)**: Norway's national
  topographic maps.
- **OpenStreetMap**: worldwide. Its tile usage policy allows light personal use only.
- **Custom**: your own service, as a tile URL with `{z}`, `{x}` and `{y}`, or a WMS GetMap URL with
  `BBOX={bbox-epsg-3857}`. An attribution is required and is always shown on the map.

The tile host must allow cross-origin requests. If Grafana's content security policy is enabled,
add the host to its `connect-src`: `https://cache.kartverket.no` for the Kartverket maps,
`https://tile.openstreetmap.org` for OpenStreetMap, or your custom service's host.

This panel needs WebGL 2. Without it, for example with hardware acceleration turned off, it shows a
message instead of a map.
