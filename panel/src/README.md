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

A position more than 10 km from the dock is left out, as no lawn reaches that far. If every position
is that far away, the X and Y columns hold something other than metres from the dock, and the panel
names them instead of drawing.

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

## Terrain

Turn on **Enable** under **Terrain** to draw the map over real relief, so a sloping garden looks
like a sloping garden. The Trail follows the ground. A switch on the panel changes between
**Flat** and **Terrain** without moving the view; **Start in** sets which one the panel opens in.
Drag with the right mouse button, or Ctrl-drag, to tilt and turn the camera; on a touch screen, use
two fingers.

Terrain is independent of the Base map, so any Base map works with any **Source**:

- **Mapterhorn** (default): worldwide, with 1 m detail in Norway from Kartverket's national
  elevation model.
- **AWS Terrain Tiles**: worldwide, with 10 m detail in Norway from the same model.
- **Custom**: your own elevation tiles, as a tile URL with `{z}`, `{x}` and `{y}`, with their
  encoding, tile size, max zoom and attribution. The encoding is Terrarium or Mapbox; tiles in an
  encoding of their own, with custom colour factors, are not supported.

## Overlay

An Overlay is drawn over the Base map and under the Trail, with an **Opacity** slider. Choose it
under **Overlay**:

- **Kartverket hillshade**: shaded relief from Norway's national elevation model, which shows the
  lie of the land in the flat view too.
- **Custom**: your own tile or WMS service, entered like a custom Base map. This is how to use
  imagery you hold a licence for, such as an orthophoto subscription: paste the URL your provider
  gave you, token included. It is saved with the dashboard, so anyone who can view the dashboard
  can read it.

## Tile hosts

Every tile host must allow cross-origin requests. If Grafana's content security policy is enabled,
add the hosts in use to its `connect-src`:

| Source                                | Host                             |
| ------------------------------------- | -------------------------------- |
| Kartverket Base maps                  | `https://cache.kartverket.no`    |
| OpenStreetMap Base map                | `https://tile.openstreetmap.org` |
| Mapterhorn Terrain                    | `https://tiles.mapterhorn.com`   |
| AWS Terrain Tiles                     | `https://s3.amazonaws.com`       |
| Kartverket hillshade Overlay          | `https://wms.geonorge.no`        |
| A custom Base map, Terrain or Overlay | the host in its URL template     |

## Browser support

This panel needs WebGL 2. Without it, for example with hardware acceleration turned off, it shows a
message instead of a map.
