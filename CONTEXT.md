# Navimow Grafana Plugin

A Grafana panel plugin and companion collector that visualise a Navimow robotic mower's work on a map of the lawn, with Kartverket data as a base-map option for Norway.

## Language

**Job**:
One mowing run, from the mower leaving the dock to returning to it.
_Avoid_: Task, session, run, mission

**Zone**:
A subdivision of the lawn that the mower works through inside a Job.
_Avoid_: Partition, boundary (SDK's `currentMowBoundary`), area

**Trail**:
The ordered stream of mower positions recorded during one Job.
_Avoid_: Track, path, route, mowermap

**Dock origin**:
The latitude/longitude of the charging dock plus the rotation of the mower's local x-axis relative to north, which georeferences a Trail onto the map.
_Avoid_: Calibration point, anchor, home

**Coverage**:
The area of the lawn a Trail has visited, derived from the Trail itself.
_Avoid_: Mowed area, progress map, and Heatmap for Coverage as a whole: a Heatmap is one of its styles

**Coverage style**:
One of the three ways Coverage is drawn: Grid (square cells), Heatmap (the cells smoothed) or Buffered line (the Trail as wide as the mower cuts).
_Avoid_: Mode, layer, rendering, visualisation

**Boundary**:
The user-drawn outline of the lawn and its Zones, as polygons.
_Avoid_: Perimeter, fence, map, lawn shape

**Lawn**:
The Dock origin and Boundary of one mower, saved together.
_Avoid_: calibration, placement, setup

## Map panel

**Base map**:
The raster tile layer drawn underneath everything else on the map panel.
_Avoid_: Background, basemap tiles, map provider

**Terrain**:
The elevation tile source (raster-dem) that gives the map its 3D relief.
_Avoid_: DEM, heightmap, elevation layer

**Overlay**:
An optional raster layer drawn between the Base map and the Trail, such as hillshade or orthophoto.
_Avoid_: Layer, extra map

**Preset**:
A built-in, named configuration for a Base map, Terrain or Overlay source.
_Avoid_: Provider, template, profile
