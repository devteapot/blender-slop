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


def build_workspace_descriptor(
    snapshot: dict[str, Any],
    actions: dict[str, Action],
    object_actions: ObjectActionFactory,
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
            "scene": _scene_descriptor(scene, actions, object_actions),
            "materials": _materials_descriptor(scene),
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
) -> dict[str, Any]:
    objects = list(scene.get("objects") or [])
    object_items = [_object_item(obj, object_actions) for obj in objects if isinstance(obj, dict)]

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
                },
            },
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


def _materials_descriptor(scene: dict[str, Any]) -> dict[str, Any]:
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
            }
            for material in materials
            if isinstance(material, dict)
        ],
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
