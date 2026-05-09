"""Pure descriptor helpers for the Blender SLOP add-on.

This module deliberately avoids importing ``bpy`` so the SLOP state projection
can be tested in a normal Python interpreter. Runtime handlers are supplied by
the Blender-facing add-on module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

Action = Callable[[dict[str, Any]], dict[str, Any]]
ObjectActionFactory = Callable[[str], dict[str, Any]]
NamedActionFactory = Callable[[str], dict[str, Any]]


def build_workspace_descriptor(
    snapshot: dict[str, Any],
    actions: dict[str, Action],
    object_actions: ObjectActionFactory,
    material_actions: NamedActionFactory | None = None,
    camera_actions: NamedActionFactory | None = None,
    light_actions: NamedActionFactory | None = None,
) -> dict[str, Any]:
    """Build the ``/blender/workspace`` descriptor from cached Blender state."""
    scene = dict(snapshot.get("scene") or {})
    objects = list(scene.get("objects") or [])
    last_result = snapshot.get("last_result")
    last_error = snapshot.get("last_error")
    running = bool(snapshot.get("running"))

    return {
        "type": "view",
        "props": {
            "label": "Blender SLOP",
            "status": "running" if running else "stopped",
            "scene": scene.get("name"),
            "object_count": scene.get("object_count", len(objects)),
            "materials_count": scene.get("materials_count", 0),
            "active_object": scene.get("active_object"),
        },
        "summary": _workspace_summary(scene, running, last_error),
        "meta": {"salience": 1.0, "pinned": True},
        "actions": {
            "refresh": {
                "handler": actions["refresh"],
                "label": "Refresh",
                "description": "Refresh the SLOP snapshot from Blender's live scene.",
                "idempotent": True,
                "estimate": "fast",
            },
        },
        "children": {
            "scene": _scene_descriptor(scene, actions, object_actions, camera_actions, light_actions),
            "materials": _materials_descriptor(scene, actions, material_actions),
            "server": _server_descriptor(snapshot, actions),
            "last_result": _last_result_descriptor(last_result, last_error),
        },
    }


def stable_id(prefix: str, value: str) -> str:
    """Return a SLOP-safe, stable node id for a Blender object name."""
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-").lower()[:36]
    return f"{prefix}_{digest}_{slug or 'item'}"


def jsonable(value: Any) -> Any:
    """Convert common Blender/Python values into JSON-safe data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return str(value)


def _workspace_summary(scene: dict[str, Any], running: bool, last_error: str | None) -> str:
    if last_error:
        return f"Blender SLOP is {'running' if running else 'stopped'}; last error: {last_error}"
    return (
        f"Blender SLOP is {'running' if running else 'stopped'}; "
        f"{scene.get('object_count', 0)} objects in {scene.get('name') or 'the active scene'}."
    )


def _scene_descriptor(
    scene: dict[str, Any],
    actions: dict[str, Action],
    object_actions: ObjectActionFactory,
    camera_actions: NamedActionFactory | None,
    light_actions: NamedActionFactory | None,
) -> dict[str, Any]:
    objects = list(scene.get("objects") or [])
    object_items = [_object_item(obj, object_actions) for obj in objects if isinstance(obj, dict)]
    cameras = [item for item in objects if isinstance(item, dict) and item.get("type") == "CAMERA"]
    lights = [item for item in objects if isinstance(item, dict) and item.get("type") == "LIGHT"]

    return {
        "type": "view",
        "props": {
            "label": scene.get("name") or "Scene",
            "frame": scene.get("frame"),
            "object_count": scene.get("object_count", len(object_items)),
            "selected_objects": scene.get("selected_objects", []),
            "active_object": scene.get("active_object"),
            "camera": scene.get("camera"),
        },
        "summary": "Live semantic snapshot of the current Blender scene.",
        "meta": {"salience": 0.95},
        "actions": {
            "execute_python": {
                "handler": actions["execute_python"],
                "label": "Execute Python",
                "description": "Run Python directly inside Blender with bpy available.",
                "dangerous": True,
                "estimate": "slow",
                "params": {
                    "code": {"type": "string", "description": "Python code to execute in Blender."},
                },
            },
            "capture_viewport": {
                "handler": actions["capture_viewport"],
                "label": "Capture Viewport",
                "description": "Capture the active 3D viewport to a temporary PNG file.",
                "idempotent": True,
                "estimate": "slow",
                "params": {
                    "max_size": {"type": "integer", "description": "Maximum image dimension in pixels."},
                },
            },
        },
        "children": {
            "objects": {
                "type": "collection",
                "props": {
                    "label": "Objects",
                    "count": scene.get("object_count", len(object_items)),
                },
                "summary": f"Showing {len(object_items)} of {scene.get('object_count', len(object_items))} scene objects.",
                "meta": {
                    "salience": 0.9,
                    "total_children": scene.get("object_count", len(object_items)),
                    "window": [0, len(object_items)],
                },
                "items": object_items,
                "actions": {
                    "create_primitive": {
                        "handler": actions["create_primitive"],
                        "label": "Create Primitive",
                        "description": "Create a Blender mesh primitive at the requested transform.",
                        "dangerous": True,
                        "estimate": "fast",
                        "params": {
                            "primitive": {
                                "type": "string",
                                "enum": ["cube", "uv_sphere", "plane", "cylinder", "cone"],
                            },
                            "name": "string",
                            "location": {"type": "array", "items": {"type": "number"}},
                            "scale": {"type": "array", "items": {"type": "number"}},
                        },
                    },
                    "import_file": {
                        "handler": actions["import_file"],
                        "label": "Import File",
                        "description": "Import a local OBJ, FBX, GLTF, GLB, STL, or PLY file into Blender.",
                        "dangerous": True,
                        "estimate": "slow",
                        "params": {
                            "filepath": {"type": "string", "description": "Absolute local path to import."},
                        },
                    },
                },
            },
            "cameras": _cameras_descriptor(scene, cameras, actions, camera_actions),
            "lights": _lights_descriptor(lights, actions, light_actions),
            "collections": _collections_descriptor(scene, actions),
            "timeline": _timeline_descriptor(scene, actions),
            "render": _render_descriptor(scene, actions),
            "world": _world_descriptor(scene, actions),
        },
    }


def _object_item(obj: dict[str, Any], object_actions: ObjectActionFactory) -> dict[str, Any]:
    name = str(obj.get("name") or "Object")
    return {
        "id": stable_id("object", name),
        "props": {
            "label": name,
            "name": name,
            "object_type": obj.get("type"),
            "location": obj.get("location"),
            "rotation": obj.get("rotation"),
            "scale": obj.get("scale"),
            "dimensions": obj.get("dimensions"),
            "visible": obj.get("visible"),
            "selected": obj.get("selected"),
            "materials": obj.get("materials", []),
        },
        "summary": f"{obj.get('type', 'Object')} named {name}",
        "meta": {"salience": 0.85 if obj.get("selected") else 0.7},
        "actions": object_actions(name),
    }


def _cameras_descriptor(
    scene: dict[str, Any],
    cameras: list[dict[str, Any]],
    actions: dict[str, Action],
    camera_actions: NamedActionFactory | None,
) -> dict[str, Any]:
    return {
        "type": "collection",
        "props": {"label": "Cameras", "count": len(cameras), "active_camera": scene.get("camera")},
        "summary": f"{len(cameras)} cameras; active camera is {scene.get('camera') or 'not set'}.",
        "meta": {"salience": 0.75, "total_children": len(cameras), "window": [0, len(cameras)]},
        "items": [
            {
                "id": stable_id("camera", str(camera.get("name") or "Camera")),
                "props": {
                    "label": camera.get("name"),
                    "name": camera.get("name"),
                    "location": camera.get("location"),
                    "rotation": camera.get("rotation"),
                    "lens": camera.get("lens"),
                    "active": camera.get("name") == scene.get("camera"),
                },
                "summary": f"Camera {camera.get('name')}",
                "actions": camera_actions(str(camera.get("name"))) if camera_actions else {},
            }
            for camera in cameras
        ],
        "actions": {
            "create_camera": {
                "handler": actions["create_camera"],
                "label": "Create Camera",
                "description": "Create a camera at the requested transform and optionally make it active.",
                "dangerous": True,
                "estimate": "fast",
                "params": {
                    "name": "string",
                    "location": {"type": "array", "items": {"type": "number"}},
                    "rotation": {"type": "array", "items": {"type": "number"}},
                    "lens": "number",
                    "make_active": "boolean",
                },
            },
        },
    }


def _lights_descriptor(
    lights: list[dict[str, Any]],
    actions: dict[str, Action],
    light_actions: NamedActionFactory | None,
) -> dict[str, Any]:
    return {
        "type": "collection",
        "props": {"label": "Lights", "count": len(lights)},
        "summary": f"{len(lights)} lights in the current scene.",
        "meta": {"salience": 0.7, "total_children": len(lights), "window": [0, len(lights)]},
        "items": [
            {
                "id": stable_id("light", str(light.get("name") or "Light")),
                "props": {
                    "label": light.get("name"),
                    "name": light.get("name"),
                    "light_type": light.get("light_type"),
                    "location": light.get("location"),
                    "energy": light.get("energy"),
                    "color": light.get("color"),
                },
                "summary": f"{light.get('light_type', 'Light')} {light.get('name')}",
                "actions": light_actions(str(light.get("name"))) if light_actions else {},
            }
            for light in lights
        ],
        "actions": {
            "create_light": {
                "handler": actions["create_light"],
                "label": "Create Light",
                "description": "Create a point, sun, spot, or area light.",
                "dangerous": True,
                "estimate": "fast",
                "params": {
                    "light_type": {"type": "string", "enum": ["POINT", "SUN", "SPOT", "AREA"]},
                    "name": "string",
                    "location": {"type": "array", "items": {"type": "number"}},
                    "energy": "number",
                    "color": {"type": "array", "items": {"type": "number"}},
                },
            },
        },
    }


def _collections_descriptor(scene: dict[str, Any], actions: dict[str, Action]) -> dict[str, Any]:
    collections = scene.get("collections") or []
    return {
        "type": "collection",
        "props": {"label": "Collections", "count": len(collections)},
        "summary": f"{len(collections)} collections in the current Blender file.",
        "meta": {"salience": 0.55, "total_children": len(collections), "window": [0, len(collections)]},
        "items": [
            {
                "id": stable_id("collection", str(collection.get("name") or "Collection")),
                "props": collection,
                "summary": f"Collection {collection.get('name')}",
            }
            for collection in collections
            if isinstance(collection, dict)
        ],
        "actions": {
            "create_collection": {
                "handler": actions["create_collection"],
                "label": "Create Collection",
                "description": "Create a new collection in the current scene.",
                "dangerous": True,
                "estimate": "fast",
                "params": {"name": "string"},
            },
            "move_object_to_collection": {
                "handler": actions["move_object_to_collection"],
                "label": "Move Object",
                "description": "Move an object into a collection, creating the collection if necessary.",
                "dangerous": True,
                "estimate": "fast",
                "params": {"object_name": "string", "collection_name": "string"},
            },
        },
    }


def _timeline_descriptor(scene: dict[str, Any], actions: dict[str, Action]) -> dict[str, Any]:
    timeline = scene.get("timeline") or {}
    return {
        "type": "context",
        "props": {"label": "Timeline", **timeline},
        "summary": f"Frame {timeline.get('frame')} of {timeline.get('frame_start')} to {timeline.get('frame_end')}.",
        "meta": {"salience": 0.65},
        "actions": {
            "set_frame": {
                "handler": actions["set_frame"],
                "label": "Set Frame",
                "description": "Set the current scene frame.",
                "idempotent": True,
                "estimate": "instant",
                "params": {"frame": "integer"},
            },
            "set_frame_range": {
                "handler": actions["set_frame_range"],
                "label": "Set Frame Range",
                "description": "Set the scene start and end frames.",
                "dangerous": True,
                "estimate": "instant",
                "params": {"frame_start": "integer", "frame_end": "integer"},
            },
        },
    }


def _render_descriptor(scene: dict[str, Any], actions: dict[str, Action]) -> dict[str, Any]:
    render = scene.get("render") or {}
    return {
        "type": "context",
        "props": {"label": "Render", **render},
        "summary": f"Render engine {render.get('engine')} at {render.get('resolution_x')}x{render.get('resolution_y')}.",
        "meta": {"salience": 0.65},
        "actions": {
            "set_render_settings": {
                "handler": actions["set_render_settings"],
                "label": "Set Render Settings",
                "description": "Set render engine, resolution, and frame rate.",
                "dangerous": True,
                "estimate": "fast",
                "params": {
                    "engine": "string",
                    "resolution_x": "integer",
                    "resolution_y": "integer",
                    "fps": "integer",
                },
            },
            "render_still": {
                "handler": actions["render_still"],
                "label": "Render Still",
                "description": "Render the current frame to a temporary PNG.",
                "dangerous": True,
                "estimate": "slow",
                "params": {"filepath": "string"},
            },
        },
    }


def _world_descriptor(scene: dict[str, Any], actions: dict[str, Action]) -> dict[str, Any]:
    world = scene.get("world") or {}
    return {
        "type": "context",
        "props": {"label": "World", **world},
        "summary": f"World color {world.get('color')}.",
        "meta": {"salience": 0.55},
        "actions": {
            "set_world_color": {
                "handler": actions["set_world_color"],
                "label": "Set World Color",
                "description": "Set the current world's viewport/background color.",
                "dangerous": True,
                "estimate": "fast",
                "params": {"rgba": {"type": "array", "items": {"type": "number"}}},
            },
        },
    }


def _materials_descriptor(
    scene: dict[str, Any],
    actions: dict[str, Action],
    material_actions: NamedActionFactory | None,
) -> dict[str, Any]:
    materials = scene.get("materials") or []
    return {
        "type": "collection",
        "props": {"label": "Materials", "count": len(materials)},
        "summary": f"{len(materials)} materials in the current Blender file.",
        "meta": {"salience": 0.55, "total_children": len(materials), "window": [0, len(materials)]},
        "items": [
            {
                "id": stable_id("material", str(material.get("name") or "Material")),
                "props": material,
                "summary": f"Material {material.get('name')}",
                "actions": material_actions(str(material.get("name"))) if material_actions else {},
            }
            for material in materials
            if isinstance(material, dict)
        ],
        "actions": {
            "create_material": {
                "handler": actions["create_material"],
                "label": "Create Material",
                "description": "Create or update a material with a diffuse color.",
                "dangerous": True,
                "estimate": "fast",
                "params": {
                    "name": "string",
                    "rgba": {"type": "array", "items": {"type": "number"}},
                },
            },
        },
    }


def _server_descriptor(snapshot: dict[str, Any], actions: dict[str, Action]) -> dict[str, Any]:
    return {
        "type": "status",
        "props": {
            "label": "SLOP Server",
            "running": bool(snapshot.get("running")),
            "url": snapshot.get("url"),
            "refresh_interval": snapshot.get("refresh_interval"),
        },
        "summary": snapshot.get("url") or "The native Blender SLOP server is stopped.",
        "meta": {"salience": 0.75},
        "actions": {
            "refresh": {
                "handler": actions["refresh"],
                "label": "Refresh",
                "description": "Refresh the provider state tree from Blender.",
                "idempotent": True,
                "estimate": "fast",
            },
        },
    }


def _last_result_descriptor(last_result: Any, last_error: str | None) -> dict[str, Any]:
    return {
        "type": "document",
        "props": {
            "label": "Last Result",
            "result": jsonable(last_result),
            "error": last_error,
        },
        "summary": _last_result_summary(last_result, last_error),
        "meta": {"salience": 0.65 if last_result or last_error else 0.25},
    }


def _last_result_summary(last_result: Any, last_error: str | None) -> str:
    if last_error:
        return f"Last Blender SLOP action failed: {last_error}"
    if last_result is None:
        return "No SLOP action has been invoked yet."
    if isinstance(last_result, dict):
        return "Last action returned: " + ", ".join(list(last_result.keys())[:6])
    return f"Last action returned {type(last_result).__name__}."
