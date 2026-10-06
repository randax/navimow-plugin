# Navimow lawn map for Grafana

A Grafana panel and a companion collector that show a Navimow robotic mower's work on a map of the
lawn, with Kartverket data as a Base map option for Norway.

| Directory                             | What it is                                                        | Licence      | Tags           |
| ------------------------------------- | ----------------------------------------------------------------- | ------------ | -------------- |
| [`panel/`](panel/README.md)           | The Grafana panel that draws a mower's Trail on a map             | Apache-2.0   | `panel/v*`     |
| [`collector/`](collector/README.md)   | The service that records a mower's Trail and Jobs into a database | GPL-3.0-only | `collector/v*` |
| [`dashboards/`](dashboards/README.md) | Dashboards over the collector's data, released with the panel     | Apache-2.0   |                |

To install the panel into your own Grafana, see [`panel/README.md`](panel/README.md#install).

## Two versions

The panel and the collector are versioned and released separately, each from tags with its own
prefix, and a release of one never forces a release of the other. They meet only in the database.
Its schema only ever gains columns, and the panel treats every column beyond a position's time, x
and y as optional, so a panel reading an older collector's data draws less rather than failing.

## Two licences

The collector is GPL-3.0-only because it imports the
[Navimow SDK](https://github.com/randax/navimow-sdk), which is. That licence is inherited, not
chosen.

The panel is Apache-2.0, the norm for Grafana plugins, and it can be: it links to nothing copyleft.
It imports neither the collector nor the SDK, and knows nothing of the mower's protocol. It draws
whatever rows Grafana's own data sources hand it, so the two programs share a database and no code.
