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
_Avoid_: Heatmap, mowed area, progress map

**Boundary**:
The user-drawn outline of the lawn and its Zones, as polygons.
_Avoid_: Perimeter, fence, map, lawn shape
