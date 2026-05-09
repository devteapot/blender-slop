# Blender SLOP

Blender SLOP is a native Blender add-on that exposes Blender as a [SLOP](https://github.com/devteapot/slop/tree/main/spec) provider. It uses [BlenderMCP](https://github.com/ahujasid/blender-mcp) as reference material for useful scene-control behaviors, but it does not run an MCP server, call a BlenderMCP socket, or depend on BlenderMCP internally.

The add-on runs inside Blender, imports the published [`slop-ai`](https://pypi.org/project/slop-ai/) Python SDK, and serves SLOP over WebSocket.

## What It Exposes

- SLOP server status and connection URL.
- Scene summary, frame, object count, materials, selected objects, active object, and camera.
- Object nodes with transform, visibility, selection, dimensions, and material state.
- Object affordances for inspect, select, transform, set material, and delete.
- Scene affordances for viewport capture and arbitrary Blender Python execution.
- Object collection affordance for creating mesh primitives.

## Install

For local development outside Blender:

```bash
uv sync --extra dev
uv run pytest
```

## Blender Setup

1. Open Blender.
2. Install the `addon/blender_slop_addon` folder as a Blender add-on. To make an installable zip:

   ```bash
   cd addon
   zip -r ../blender_slop_addon.zip blender_slop_addon
   ```

   Then use `Edit > Preferences > Add-ons > Install...`.
3. Enable `Interface: Blender SLOP`.
4. In the 3D View sidebar, open the `Blender SLOP` tab.
5. Click `Install Dependencies` once to install `slop-ai[websocket]` into Blender's Python environment.
6. Restart Blender or disable and re-enable the add-on if Blender cannot import the dependency immediately.
7. Click `Start SLOP Provider`.

By default the provider listens on:

```text
ws://127.0.0.1:8765/slop
```

## State Shape

The root provider id is `blender`. The main app node is:

```text
/blender/workspace
```

Useful paths include:

```text
/blender/workspace/scene
/blender/workspace/scene/objects
/blender/workspace/materials
/blender/workspace/server
/blender/workspace/last_result
```

All actions are SLOP affordances attached to the node they operate on. Dangerous actions such as `execute_python`, object transforms, material edits, primitive creation, and deletion are marked `dangerous`.

## Security Notes

This provider can run arbitrary Python inside Blender because it intentionally exposes that SLOP affordance. Treat SLOP consumers as untrusted, review dangerous affordances before invocation, and save Blender work before running destructive scene operations.

## Attribution

BlenderMCP was used as reference material and is MIT licensed. This repository does not vendor BlenderMCP code. See `NOTICE`.
