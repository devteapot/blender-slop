# Blender SLOP

Blender SLOP ports [BlenderMCP](https://github.com/ahujasid/blender-mcp) into a [SLOP](https://github.com/devteapot/slop/tree/main/spec) provider. It keeps BlenderMCP's existing Blender add-on socket protocol, then projects Blender state as a semantic SLOP tree with contextual affordances.

The provider is implemented in Python with the published [`slop-ai`](https://pypi.org/project/slop-ai/) SDK.

## What It Exposes

- Connection status for the BlenderMCP socket add-on.
- Scene summary, object count, material count, and the first objects returned by BlenderMCP.
- Object-level `inspect` affordances for detailed material, mesh, transform, and bounding-box data.
- Scene affordances for refresh, viewport capture, and arbitrary Blender Python execution.
- Poly Haven, Sketchfab, Hyper3D Rodin, and Hunyuan3D affordances when the Blender add-on supports them.
- A raw command escape hatch for BlenderMCP commands that are not yet modeled as first-class SLOP nodes.

## Install

```bash
uv tool install .
```

For local development:

```bash
uv sync --extra dev
uv run pytest
```

## Blender Setup

1. Open Blender.
2. Install `addon/blender_slop_addon.py` from this repository through `Edit > Preferences > Add-ons > Install...`.
3. Enable the add-on.
4. In the 3D View sidebar, open the `BlenderMCP` tab and click `Connect to Claude`.

The button name comes from upstream BlenderMCP, but this provider talks to the same local socket.

By default the add-on listens on `localhost:9876`. Override this provider with:

```bash
export BLENDER_HOST=localhost
export BLENDER_PORT=9876
```

## Run As A SLOP Provider

Stdio transport:

```bash
blender-slop stdio
```

WebSocket transport:

```bash
blender-slop websocket --host 127.0.0.1 --port 8765 --path /slop
```

If the provider should start before Blender is ready:

```bash
blender-slop --no-connect-on-start websocket
```

## State Shape

The root provider id is `blender`. The main app node is:

```text
/blender/workspace
```

Useful paths include:

```text
/blender/workspace/connection
/blender/workspace/scene
/blender/workspace/scene/objects
/blender/workspace/integrations/polyhaven
/blender/workspace/integrations/sketchfab
/blender/workspace/integrations/hyper3d
/blender/workspace/integrations/hunyuan3d
/blender/workspace/commands
```

All actions are SLOP affordances attached to the node they operate on. Dangerous actions such as `execute_python`, asset downloads, model imports, and raw commands are marked `dangerous`.

## Security Notes

This provider can run arbitrary Python inside Blender because BlenderMCP exposes that capability. Treat SLOP consumers as untrusted, review dangerous affordances before invocation, and save Blender work before running destructive scene operations.

## Attribution

The Blender add-on copy in `addon/blender_slop_addon.py` comes from [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp), licensed MIT. See `NOTICE`.
