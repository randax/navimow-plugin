# Navimow Map

Draws a Navimow robotic mower's Trail on a real map of the lawn.

## Trail data

The panel reads one query with a row per mower position. With a SQL data source, set the query to
**Format as: Table**: the Time series format turns text columns such as `job_id` and `zone` into
labels, which the panel does not read.

| Column  | Default name | Required | Meaning                                                                                                    |
| ------- | ------------ | -------- | ---------------------------------------------------------------------------------------------------------- |
| Time    | `time`       | yes      | When the position was recorded                                                                             |
| X, Y    | `x`, `y`     | yes      | Metres from the charging dock, on the mower's own axes                                                     |
| Heading | `theta`      | no       | Radians counter-clockwise from the x-axis                                                                  |
| Job     | `job_id`     | no       | Each Job is drawn as its own line, in its own colour                                                       |
| Zone    | `zone`       | no       | The Zone the position was mowed in; shown when hovering the Trail in an upcoming release                   |
| Status  | `status`     | no       | The mower's state, such as mowing or returning; shown when hovering the Trail in an upcoming release       |
| Mower   | `device_id`  | no       | Which mower reported the position; used to warn about data from more than one mower in an upcoming release |

If your columns are named differently, set their names under **Trail columns** in the panel
options. A missing optional column means less is drawn. Without a Job column, each query result is
one Trail; with a Mower column, each mower's Jobs are kept apart. A silence of more than 15 minutes,
or a row without a position, is drawn as a gap rather than a straight line.

The mower is drawn at its last position, as an arrow when the heading is known. A position older
than 15 minutes is faded and labelled with its age, such as "Last seen 3 h ago".

## Dock origin

The mower reports metres from its dock, not coordinates, so the panel needs to know where the dock
is. Under **Dock origin**, enter the dock's latitude and longitude, and the rotation: the compass
bearing of the mower's x-axis, in degrees clockwise from north. Adjust the rotation until the Trail
lies on the lawn.

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
