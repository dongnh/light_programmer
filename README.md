# Light Programmer

Sensor-driven home lighting over Matter.

## Overview

Light Programmer is the schedule brain of a small Matter home. You describe how each room should feel across the day — brightness, colour temperature, which sensors matter — and a single daemon keeps the lights matching that description. It replaces wall switches with intent.

It is built for home use. It is not hardened for commercial deployment.

## How it works

A 1 Hz loop reads your configuration, interpolates each light's circadian schedule between the points you set, and gates the result on occupancy. Sensors can be combined as a simple any-of list or as a small boolean expression tree with `AND`, `OR`, `NOT`, and time windows. Commands go out only when the target meaningfully differs from cached state, so the Matter fabric stays quiet.

An optional rain override layers weather onto an already-on light — useful for an artificial skylight that should go overcast when it rains outside. Intensity from the rain sensor scales brightness and shifts colour temperature; with `effect: "flow"` the loop hands the bulb to the Yeelight bridge for an on-device flicker animation and steps back until the weather clears.

Two runtime flags, `auto` and `kill`, live in a shared JSON file and are reachable over a small HTTP endpoint. `auto` pauses the schedule and leaves devices as they are. `kill` forces every light off and holds. The same surface drives the Auto and Kill switches the HomeKit bridge exposes to Apple Home.

Air conditioning is out of scope. AC and climate now live in matter_webcontrol and the HomeKit bridges; the MCP server here still proxies their read and write APIs for convenience, but the schedule loop never touches them.

An optional MCP server lets Claude and other agents enumerate devices, read climate and AC state, edit the configuration with validation, drive lights directly, and toggle the mode flags.

## Installation

Requires Python 3.8 or later and a reachable [matter_webcontrol](https://github.com/dongnh/matter_webcontrol) instance.

```
pip install light-programmer
```

The MCP server ships as the `mcp` extra. Entry points, dependencies, and version are declared in `pyproject.toml`.

## Configuration

A configuration is a JSON array, one entry per light. Each entry carries an id, an optional note, a schedule of `{time, level, kelvin}` points, and either a flat `sensor` list with per-sensor timeouts or a `sensor_condition` expression. A `rain` block is optional. See `sample.json` for a working multi-room example, and `light_programmer/programmer.py` for the authoritative reader — the schema is informal and unknown keys are silently ignored.

`light-genconfig` will scaffold a starter file from your controller's `/api/metadata`.

## Related projects

Light Programmer is one piece of a larger Matter home. Its peers live under [github.com/dongnh](https://github.com/dongnh):

- [matter_webcontrol](https://github.com/dongnh/matter_webcontrol) — the required runtime peer. Speaks Matter so this daemon does not have to.
- matter-weather-sensor — exposes rain, illuminance, and climate as Matter sensors. Feeds the rain override.
- The Yeelight bridge — target for `effect: "flow"` on-device colour-flow animation.
- homekit-bridge — consumes `/mode` to surface Auto and Kill as Apple Home switches.
- matter-homekit-bridge — brings HomeKit-only Aqara AC and heater units into the same fabric.

## License

MIT.
