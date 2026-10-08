# Navimow Map

Draws a Navimow robotic mower's Trail on a real map of the lawn.

## Trail data

The panel reads one query with a row per mower position. With a SQL data source, the query may be set
to **Format as** Table or Time series. The Time series format turns text columns such as `job_id`
into labels and names the time column `Time`: the panel reads a column that is not there from the
label of its name, and the time from the one time column there is when none has the name.

| Column  | Default name | Required | Meaning                                                                        |
| ------- | ------------ | -------- | ------------------------------------------------------------------------------ |
| Time    | `time`       | yes      | When the position was recorded                                                 |
| X, Y    | `x`, `y`     | yes      | Metres from the charging dock, on the mower's own axes                         |
| Heading | `theta`      | no       | Radians counter-clockwise from the x-axis                                      |
| Job     | `job_id`     | no       | Each Job is drawn as its own line, in its own colour                           |
| Zone    | `zone`       | no       | The Zone the position was mowed in; shown when hovering the Trail              |
| Status  | `status`     | no       | The mower's state, such as mowing or returning; shown when hovering the Trail  |
| Mower   | `device_id`  | no       | Which mower reported the position; the panel warns when there is more than one |

If your columns are named differently, set their names under **Trail columns** in the panel
options. A missing optional column means less is drawn. Without a Job column, each query result is
one Trail; with a Mower column, each mower's Jobs are kept apart. A silence of more than 15 minutes,
or a row without a position, is drawn as a gap rather than a straight line.

A position more than 10 km from the dock is left out, as no lawn reaches that far. If every position
is that far away, the X and Y columns hold something other than metres from the dock, and the panel
names them instead of drawing.

The mower is drawn at its last position, as an arrow when the heading is known. A position older
than 15 minutes is faded and labelled with its age, such as "Last seen 3 h ago".

A panel shows one mower's lawn. If the query returns positions from more than one mower, they are
all drawn, with a warning that names the mowers: give each mower a panel of its own, and narrow each
panel's query to its mower.

## Hovering and selecting

Hovering a Trail shows the time, Job, Zone and status of the position under the pointer, in the
dashboard's time zone. Hovering a Zone of the Boundary shows its name and its latest progress.

Clicking a Trail selects its Job across the dashboard, by setting a dashboard variable. Add a
variable for the Job to the dashboard and choose it as **Job variable** under **Jobs** in the panel
options; the default is a variable named `job`. While the variable holds a Job, or several, the map
draws those alone, and says so if they have no positions in the time range; set to **All**, or
empty, it draws every Job in the range. On a dashboard without the variable, every Job is drawn and
a click selects nothing.

## Zone progress

A second, optional query colours the Zones traced in the Boundary by how far the mower has got
through each, from a pale wash at 0 to a deep one at 100. A Zone with no progress reported is drawn
as it was.

| Column   | Default name | Required | Meaning                                                              |
| -------- | ------------ | -------- | -------------------------------------------------------------------- |
| Zone     | `zone`       | yes      | The Zone's identifier, as given to it when it was traced             |
| Progress | `progress`   | yes      | How far through the Zone the mower is, from 0 to 100                 |
| Time     | `time`       | no       | When that was reported; the latest row of each Zone is the one shown |

Set other column names under **Zone progress columns**. The panel tells the two queries apart by
their columns, so their order does not matter: any query result with a Zone and a progress column is
read as Zone progress.

## Coverage

Coverage shows where the mower has cut, worked out in the panel from the same positions as the
Trail: nothing extra to query. The **Coverage** button on the panel switches between three styles:

- **Grid** (default): the lawn as square cells, coloured by how often each was cut. The patch that
  is never cut stays empty, and the ground that is cut again and again goes dark.
- **Heatmap**: the same counts, smoothed into a glow.
- **Buffered line**: the Trail drawn as wide as the mower cuts, darker where passes cross.

**Raised**, on the same button, stands Coverage up from the ground and tilts the map to show it:
cells become columns, the Heatmap a surface, the Buffered line a slab. The button also holds the key
to the colours. While Coverage is shown the Trail is drawn as a faint hairline over it, so that it
does not paint over what lies beneath; hide Coverage under **Show or hide** to have the Trail back
in full.

Under **Coverage** in the panel options:

- **Style** and **Raised** set how the panel opens; the button changes it from there, for as long
  as the dashboard is open.
- **Colour by** chooses what colour and height tell: **Visit count**, or **Time since mowed**,
  measured back from the latest position in the time range. Only the Grid shows either. A Heatmap
  shows visit count only, and a raised Buffered line shows where the mower has cut but not how
  often; the panel says so on the map when a style cannot show what is asked for.
- **Cell size** is the side of a Grid cell, 0.5 m unless changed, from 0.25 m to 5 m. Smaller is
  finer and slower to draw.
- **Cutting width** is the width of the mower's cutting deck, 0.43 m unless changed.

A cell counts as cut when the mower passes over it, on the line between one position and the next:
positions alone are too far apart to fill a grid. A second pass over a cell is a second visit;
standing on it is not. Positions more than 20 seconds apart are not joined, so a gap in the data is
a gap in Coverage, and a mower sitting in its dock, which reports every few minutes, adds nothing.

The Grid and the Heatmap stay light however long the time range, as a lawn has only so many cells.
A Buffered line is a shape for every step the mower took: beyond 20,000 steps, about five Jobs, it
draws the latest ones and says so.

## Controls on the panel

The buttons at the top right of the map steer the view. None of them changes what is saved.

- **Zoom in** and **Zoom out**.
- **Turn north up**: the needle shows where north is, and a click turns the map back to it.
- **Fit to Trail**: brings the whole Trail back into view after panning or zooming away.
- **Follow the mower**: puts the mower back in the middle of the map on every refresh, and leaves
  the zoom to you. It is off unless **Follow the mower** under **Map view** in the panel options
  starts it on, as for a wall display, so a refresh does not take a map you are panning around back
  to the mower. It needs a mower on the map to follow.
- **Show or hide**: the Trail, Coverage and the Boundary, each on its own.
- **Coverage**: the style Coverage is drawn in, flat or raised, and the key to its colours.

Use the dashboard's time picker to look at another period; the panel has no time control of its own.

## Dock origin

The mower reports metres from its dock, not coordinates, so the panel needs to know where the dock
is. Under **Dock origin and Boundary**, enter the dock's latitude and longitude, and the rotation:
the compass bearing of the mower's x-axis, in degrees clockwise from north. Adjust the rotation
until the Trail lies on the lawn.

**Calibrate on the map** opens the same values on a map of their own: drag the dock into place,
turn the Trail onto the lawn, and trace the lawn's outline and its Zones as the Boundary. The fields
and the map edit one saved value, so either can be used at any time.

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

Every tile host must allow cross-origin requests. Grafana's content security policy is off by
default. If it is enabled, the map stays blank until the hosts in use are added to the `connect-src`
of `content_security_policy_template`, under `[security]` in `grafana.ini`:

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
