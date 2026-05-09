"""Native Blender SLOP provider add-on.

The add-on hosts a SLOP WebSocket provider inside Blender and calls ``bpy``
directly. BlenderMCP is used as reference material for useful scene-control
behaviors, but no MCP server or BlenderMCP socket protocol is used here.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import bpy
    import mathutils
except ImportError:  # Allows provider_core tests outside Blender.
    bpy = None
    mathutils = None

from .provider_core import build_workspace_descriptor, jsonable


bl_info = {
    "name": "Blender SLOP",
    "author": "Codex",
    "version": (0, 2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Blender SLOP",
    "description": "Expose Blender as a native SLOP provider.",
    "category": "Interface",
}


DEPENDENCY_SPEC = "slop-ai[websocket]>=0.2.0,<0.3.0"
_runtime: "BlenderSlopRuntime | None" = None


@dataclass
class _MainThreadTask:
    callback: Any
    event: threading.Event
    result: Any = None
    error: BaseException | None = None


class BlenderSlopRuntime:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        path: str,
        refresh_interval: float,
        allowed_origins: str,
    ) -> None:
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self.refresh_interval = refresh_interval
        self.allowed_origins = [item.strip() for item in allowed_origins.split(",") if item.strip()]
        self.url = f"ws://{self.host}:{self.port}{self.path}"

        self._main_thread_id = threading.get_ident()
        self._tasks: queue.Queue[_MainThreadTask] = queue.Queue()
        self._snapshot_lock = threading.RLock()
        self._snapshot: dict[str, Any] = self._empty_snapshot()
        self._last_refresh = 0.0

        self._SlopServer = None
        self._serve_websocket = None
        self._slop = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def status_text(self) -> str:
        return f"Running at {self.url}" if self._running else "Stopped"

    def start(self) -> None:
        if self._running:
            return
        self._import_slop()
        self._slop = self._create_slop_server()
        self._running = True
        self.refresh_snapshot(push=False)
        self._thread = threading.Thread(target=self._thread_main, name="BlenderSLOP", daemon=True)
        self._thread.start()
        _ensure_timer()

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        server = self._server
        if loop is not None and loop.is_running():
            async def shutdown() -> None:
                if server is not None:
                    server.close()
                    await server.wait_closed()
                loop.stop()

            future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
            with contextlib.suppress(Exception):
                future.result(timeout=3)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        if self._slop is not None:
            with contextlib.suppress(Exception):
                self._slop.stop()
        self._server = None
        self._loop = None
        self._thread = None
        with self._snapshot_lock:
            self._snapshot["running"] = False

    def tick(self) -> None:
        self._drain_tasks()
        now = time.monotonic()
        if self._running and now - self._last_refresh >= self.refresh_interval:
            self.refresh_snapshot(push=True)

    def refresh_snapshot(self, *, push: bool = True) -> dict[str, Any]:
        scene = self._call_main(self._collect_scene_snapshot)
        with self._snapshot_lock:
            self._snapshot.update(
                {
                    "running": self._running,
                    "url": self.url,
                    "refresh_interval": self.refresh_interval,
                    "scene": scene,
                }
            )
        self._last_refresh = time.monotonic()
        if push:
            self._refresh_slop_on_loop()
        return {"scene": scene, "url": self.url}

    def _import_slop(self) -> None:
        try:
            from slop_ai import SlopServer
            from slop_ai.transports.websocket import serve
        except ImportError as exc:
            raise RuntimeError(
                f"Missing dependency {DEPENDENCY_SPEC}. Use the Install Dependencies button, "
                "then restart Blender or disable and re-enable this add-on."
            ) from exc
        self._SlopServer = SlopServer
        self._serve_websocket = serve

    def _create_slop_server(self) -> Any:
        assert self._SlopServer is not None
        slop = self._SlopServer("blender", "Blender")

        @slop.node("workspace")
        def workspace_node() -> dict[str, Any]:
            with self._snapshot_lock:
                snapshot = dict(self._snapshot)
            return build_workspace_descriptor(
                snapshot,
                self._workspace_actions(),
                self._object_actions,
                self._material_actions,
                self._camera_actions,
                self._light_actions,
            )

        return slop

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._start_websocket_server())
            loop.run_forever()
        except Exception as exc:
            self._record_error(exc)
        finally:
            loop.close()

    async def _start_websocket_server(self) -> None:
        assert self._serve_websocket is not None
        assert self._slop is not None
        self._server = await self._serve_websocket(
            self._slop,
            host=self.host,
            port=self.port,
            path=self.path,
            allowed_origins=self.allowed_origins or None,
        )

    def _workspace_actions(self) -> dict[str, Any]:
        return {
            "refresh": lambda _params: self._action("refresh", lambda: self.refresh_snapshot(push=False), refresh=False),
            "execute_python": lambda params: self._action("execute_python", lambda: self._execute_python(params["code"])),
            "capture_viewport": lambda params: self._action(
                "capture_viewport",
                lambda: self._capture_viewport(int(params["max_size"])),
                refresh=False,
            ),
            "create_primitive": lambda params: self._action(
                "create_primitive",
                lambda: self._create_primitive(params),
            ),
            "import_file": lambda params: self._action("import_file", lambda: self._import_file(params["filepath"])),
            "create_camera": lambda params: self._action("create_camera", lambda: self._create_camera(params)),
            "create_light": lambda params: self._action("create_light", lambda: self._create_light(params)),
            "create_collection": lambda params: self._action(
                "create_collection",
                lambda: self._create_collection(params["name"]),
            ),
            "move_object_to_collection": lambda params: self._action(
                "move_object_to_collection",
                lambda: self._move_object_to_collection(params["object_name"], params["collection_name"]),
            ),
            "set_frame": lambda params: self._action("set_frame", lambda: self._set_frame(params["frame"])),
            "set_frame_range": lambda params: self._action(
                "set_frame_range",
                lambda: self._set_frame_range(params["frame_start"], params["frame_end"]),
            ),
            "set_render_settings": lambda params: self._action(
                "set_render_settings",
                lambda: self._set_render_settings(params),
            ),
            "render_still": lambda params: self._action("render_still", lambda: self._render_still(params["filepath"])),
            "set_world_color": lambda params: self._action(
                "set_world_color",
                lambda: self._set_world_color(params["rgba"]),
            ),
            "create_material": lambda params: self._action("create_material", lambda: self._create_material(params)),
        }

    def _object_actions(self, object_name: str) -> dict[str, Any]:
        return {
            "inspect": {
                "handler": lambda _params: self._action(
                    "inspect_object",
                    lambda: self._inspect_object(object_name),
                    refresh=False,
                ),
                "label": "Inspect",
                "description": "Read detailed Blender object data including mesh counts and bounding box.",
                "idempotent": True,
                "estimate": "fast",
            },
            "select": {
                "handler": lambda _params: self._action("select_object", lambda: self._select_object(object_name)),
                "label": "Select",
                "description": "Select this object in Blender.",
                "estimate": "fast",
            },
            "rename": {
                "handler": lambda params: self._action("rename_object", lambda: self._rename_object(object_name, params["new_name"])),
                "label": "Rename",
                "description": "Rename this object.",
                "dangerous": True,
                "estimate": "instant",
                "params": {"new_name": "string"},
            },
            "duplicate": {
                "handler": lambda params: self._action(
                    "duplicate_object",
                    lambda: self._duplicate_object(object_name, params["new_name"], params["linked"]),
                ),
                "label": "Duplicate",
                "description": "Duplicate this object, optionally sharing mesh data.",
                "dangerous": True,
                "estimate": "fast",
                "params": {"new_name": "string", "linked": "boolean"},
            },
            "set_visibility": {
                "handler": lambda params: self._action(
                    "set_visibility",
                    lambda: self._set_visibility(object_name, params["visible"], params["render_visible"]),
                ),
                "label": "Set Visibility",
                "description": "Set viewport and render visibility for this object.",
                "dangerous": True,
                "estimate": "instant",
                "params": {"visible": "boolean", "render_visible": "boolean"},
            },
            "transform": {
                "handler": lambda params: self._action(
                    "transform_object",
                    lambda: self._transform_object(object_name, params),
                ),
                "label": "Transform",
                "description": "Set object location, rotation Euler, and scale.",
                "dangerous": True,
                "estimate": "fast",
                "params": {
                    "location": {"type": "array", "items": {"type": "number"}},
                    "rotation": {"type": "array", "items": {"type": "number"}},
                    "scale": {"type": "array", "items": {"type": "number"}},
                },
            },
            "set_material": {
                "handler": lambda params: self._action(
                    "set_material",
                    lambda: self._set_material(object_name, params),
                ),
                "label": "Set Material",
                "description": "Create or update a material and assign it to this object.",
                "dangerous": True,
                "estimate": "fast",
                "params": {
                    "material_name": "string",
                    "rgba": {"type": "array", "items": {"type": "number"}},
                },
            },
            "add_modifier": {
                "handler": lambda params: self._action(
                    "add_modifier",
                    lambda: self._add_modifier(object_name, params),
                ),
                "label": "Add Modifier",
                "description": "Add a common Blender modifier to this object.",
                "dangerous": True,
                "estimate": "fast",
                "params": {
                    "modifier_type": {
                        "type": "string",
                        "enum": ["BEVEL", "SUBSURF", "SOLIDIFY", "ARRAY", "MIRROR", "WEIGHTED_NORMAL"],
                    },
                    "name": "string",
                },
            },
            "apply_modifier": {
                "handler": lambda params: self._action(
                    "apply_modifier",
                    lambda: self._apply_modifier(object_name, params["modifier_name"]),
                ),
                "label": "Apply Modifier",
                "description": "Apply a modifier destructively.",
                "dangerous": True,
                "estimate": "slow",
                "params": {"modifier_name": "string"},
            },
            "keyframe_transform": {
                "handler": lambda params: self._action(
                    "keyframe_transform",
                    lambda: self._keyframe_transform(object_name, params["frame"]),
                ),
                "label": "Keyframe Transform",
                "description": "Insert location, rotation, and scale keyframes for this object.",
                "dangerous": True,
                "estimate": "fast",
                "params": {"frame": "integer"},
            },
            "delete": {
                "handler": lambda _params: self._action("delete_object", lambda: self._delete_object(object_name)),
                "label": "Delete",
                "description": "Delete this object from the Blender scene.",
                "dangerous": True,
                "estimate": "fast",
            },
        }

    def _material_actions(self, material_name: str) -> dict[str, Any]:
        return {
            "update": {
                "handler": lambda params: self._action("update_material", lambda: self._update_material(material_name, params)),
                "label": "Update",
                "description": "Update this material's diffuse color.",
                "dangerous": True,
                "estimate": "fast",
                "params": {"rgba": {"type": "array", "items": {"type": "number"}}},
            },
        }

    def _camera_actions(self, camera_name: str) -> dict[str, Any]:
        return {
            "set_active": {
                "handler": lambda _params: self._action("set_active_camera", lambda: self._set_active_camera(camera_name)),
                "label": "Set Active",
                "description": "Make this camera the scene's active render camera.",
                "dangerous": True,
                "estimate": "instant",
            },
        }

    def _light_actions(self, light_name: str) -> dict[str, Any]:
        return {
            "update": {
                "handler": lambda params: self._action("update_light", lambda: self._update_light(light_name, params)),
                "label": "Update",
                "description": "Update this light's energy and color.",
                "dangerous": True,
                "estimate": "fast",
                "params": {
                    "energy": "number",
                    "color": {"type": "array", "items": {"type": "number"}},
                },
            },
        }

    def _action(self, name: str, callback: Any, *, refresh: bool = True) -> dict[str, Any]:
        try:
            result = self._call_main(callback)
            result = jsonable(result)
            with self._snapshot_lock:
                self._snapshot["last_result"] = {"action": name, "data": result}
                self._snapshot["last_error"] = None
            if refresh:
                self.refresh_snapshot(push=False)
            return {"action": name, "result": result}
        except Exception as exc:
            self._record_error(exc, action=name)
            raise

    def _call_main(self, callback: Any, timeout: float = 180.0) -> Any:
        if threading.get_ident() == self._main_thread_id:
            return callback()
        task = _MainThreadTask(callback=callback, event=threading.Event())
        self._tasks.put(task)
        if not task.event.wait(timeout):
            raise TimeoutError("Timed out waiting for Blender main thread")
        if task.error is not None:
            raise task.error
        return task.result

    def _drain_tasks(self) -> None:
        while True:
            try:
                task = self._tasks.get_nowait()
            except queue.Empty:
                return
            try:
                task.result = task.callback()
            except BaseException as exc:
                task.error = exc
            finally:
                task.event.set()

    def _refresh_slop_on_loop(self) -> None:
        if self._loop is None or self._slop is None or not self._loop.is_running():
            return
        self._loop.call_soon_threadsafe(self._slop.refresh)

    def _record_error(self, exc: BaseException, *, action: str | None = None) -> None:
        with self._snapshot_lock:
            self._snapshot["last_error"] = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self._snapshot["last_result"] = {"action": action, "data": None} if action else self._snapshot.get("last_result")

    def _empty_snapshot(self) -> dict[str, Any]:
        return {
            "running": False,
            "url": self.url,
            "refresh_interval": self.refresh_interval,
            "scene": {},
            "last_result": None,
            "last_error": None,
        }

    def _collect_scene_snapshot(self) -> dict[str, Any]:
        objects = [_object_summary(obj) for obj in list(bpy.context.scene.objects)[:50]]
        materials = [_material_summary(mat) for mat in list(bpy.data.materials)[:50]]
        collections = [_collection_summary(collection) for collection in list(bpy.data.collections)[:50]]
        active = bpy.context.view_layer.objects.active
        return {
            "name": bpy.context.scene.name,
            "frame": bpy.context.scene.frame_current,
            "object_count": len(bpy.context.scene.objects),
            "materials_count": len(bpy.data.materials),
            "camera": bpy.context.scene.camera.name if bpy.context.scene.camera else None,
            "active_object": active.name if active else None,
            "selected_objects": [obj.name for obj in bpy.context.selected_objects],
            "objects": objects,
            "materials": materials,
            "collections": collections,
            "timeline": {
                "frame": bpy.context.scene.frame_current,
                "frame_start": bpy.context.scene.frame_start,
                "frame_end": bpy.context.scene.frame_end,
                "fps": bpy.context.scene.render.fps,
            },
            "render": {
                "engine": bpy.context.scene.render.engine,
                "resolution_x": bpy.context.scene.render.resolution_x,
                "resolution_y": bpy.context.scene.render.resolution_y,
                "resolution_percentage": bpy.context.scene.render.resolution_percentage,
                "fps": bpy.context.scene.render.fps,
                "filepath": bpy.context.scene.render.filepath,
            },
            "world": {
                "name": bpy.context.scene.world.name if bpy.context.scene.world else None,
                "color": _rounded_vector(bpy.context.scene.world.color) if bpy.context.scene.world else None,
            },
        }

    def _execute_python(self, code: str) -> dict[str, Any]:
        namespace = {"bpy": bpy, "mathutils": mathutils, "result": None}
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            exec(code, namespace)
        return {"stdout": capture.getvalue(), "result": jsonable(namespace.get("result"))}

    def _capture_viewport(self, max_size: int) -> dict[str, Any]:
        area = next((item for item in bpy.context.screen.areas if item.type == "VIEW_3D"), None)
        if area is None:
            raise RuntimeError("No active 3D viewport found")
        handle = tempfile.NamedTemporaryFile(prefix="blender-slop-viewport-", suffix=".png", delete=False)
        filepath = handle.name
        handle.close()
        with bpy.context.temp_override(area=area):
            bpy.ops.screen.screenshot_area(filepath=filepath)
        image = bpy.data.images.load(filepath)
        try:
            width, height = image.size
            if max(width, height) > max_size:
                scale = max_size / max(width, height)
                image.scale(max(1, int(width * scale)), max(1, int(height * scale)))
                image.file_format = "PNG"
                image.save()
                width, height = image.size
        finally:
            bpy.data.images.remove(image)
        return {"filepath": filepath, "width": width, "height": height}

    def _create_primitive(self, params: dict[str, Any]) -> dict[str, Any]:
        primitive = params["primitive"]
        location = _vector(params["location"], "location")
        scale = _vector(params["scale"], "scale")
        name = params["name"].strip() or primitive
        if primitive == "cube":
            bpy.ops.mesh.primitive_cube_add(size=1, location=location)
        elif primitive == "uv_sphere":
            bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, radius=0.5, location=location)
        elif primitive == "plane":
            bpy.ops.mesh.primitive_plane_add(size=1, location=location)
        elif primitive == "cylinder":
            bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=0.5, depth=1, location=location)
        elif primitive == "cone":
            bpy.ops.mesh.primitive_cone_add(vertices=32, radius1=0.5, radius2=0, depth=1, location=location)
        else:
            raise ValueError(f"Unsupported primitive: {primitive}")
        obj = bpy.context.object
        obj.name = name
        obj.scale = scale
        return _object_summary(obj, detailed=True)

    def _import_file(self, filepath: str) -> dict[str, Any]:
        path = Path(filepath).expanduser()
        if not path.exists():
            raise ValueError(f"File does not exist: {filepath}")
        before = set(bpy.data.objects.keys())
        suffix = path.suffix.lower()
        if suffix == ".obj":
            if hasattr(bpy.ops.wm, "obj_import"):
                bpy.ops.wm.obj_import(filepath=str(path))
            else:
                bpy.ops.import_scene.obj(filepath=str(path))
        elif suffix == ".fbx":
            bpy.ops.import_scene.fbx(filepath=str(path))
        elif suffix in {".gltf", ".glb"}:
            bpy.ops.import_scene.gltf(filepath=str(path))
        elif suffix == ".stl":
            if hasattr(bpy.ops.wm, "stl_import"):
                bpy.ops.wm.stl_import(filepath=str(path))
            else:
                bpy.ops.import_mesh.stl(filepath=str(path))
        elif suffix == ".ply":
            if hasattr(bpy.ops.wm, "ply_import"):
                bpy.ops.wm.ply_import(filepath=str(path))
            else:
                bpy.ops.import_mesh.ply(filepath=str(path))
        else:
            raise ValueError(f"Unsupported import type: {suffix}")
        imported = sorted(set(bpy.data.objects.keys()) - before)
        return {"filepath": str(path), "imported_objects": imported}

    def _create_camera(self, params: dict[str, Any]) -> dict[str, Any]:
        bpy.ops.object.camera_add(location=_vector(params["location"], "location"), rotation=_vector(params["rotation"], "rotation"))
        obj = bpy.context.object
        obj.name = params["name"].strip() or obj.name
        obj.data.name = f"{obj.name}_Camera"
        obj.data.lens = float(params["lens"])
        if params["make_active"]:
            bpy.context.scene.camera = obj
        return _object_summary(obj, detailed=True)

    def _create_light(self, params: dict[str, Any]) -> dict[str, Any]:
        light_type = params["light_type"]
        bpy.ops.object.light_add(type=light_type, location=_vector(params["location"], "location"))
        obj = bpy.context.object
        obj.name = params["name"].strip() or obj.name
        obj.data.name = f"{obj.name}_Light"
        obj.data.energy = float(params["energy"])
        obj.data.color = _rgb(params["color"])
        return _object_summary(obj, detailed=True)

    def _create_collection(self, name: str) -> dict[str, Any]:
        collection_name = name.strip()
        if not collection_name:
            raise ValueError("Collection name is required")
        collection = bpy.data.collections.get(collection_name)
        if collection is None:
            collection = bpy.data.collections.new(collection_name)
            bpy.context.scene.collection.children.link(collection)
        return _collection_summary(collection)

    def _move_object_to_collection(self, object_name: str, collection_name: str) -> dict[str, Any]:
        obj = _get_object(object_name)
        collection = bpy.data.collections.get(collection_name) or bpy.data.collections.new(collection_name)
        if bpy.context.scene.collection.children.get(collection.name) is None:
            with contextlib.suppress(RuntimeError):
                bpy.context.scene.collection.children.link(collection)
        for existing in list(obj.users_collection):
            existing.objects.unlink(obj)
        collection.objects.link(obj)
        return {"object": obj.name, "collection": collection.name}

    def _inspect_object(self, name: str) -> dict[str, Any]:
        obj = _get_object(name)
        return _object_summary(obj, detailed=True)

    def _select_object(self, name: str) -> dict[str, Any]:
        obj = _get_object(name)
        for item in bpy.context.selected_objects:
            item.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        return {"selected": obj.name}

    def _rename_object(self, name: str, new_name: str) -> dict[str, Any]:
        obj = _get_object(name)
        clean = new_name.strip()
        if not clean:
            raise ValueError("new_name is required")
        old_name = obj.name
        obj.name = clean
        return {"old_name": old_name, "new_name": obj.name}

    def _duplicate_object(self, name: str, new_name: str, linked: bool) -> dict[str, Any]:
        obj = _get_object(name)
        duplicate = obj.copy()
        duplicate.data = obj.data if linked or obj.data is None else obj.data.copy()
        duplicate.name = new_name.strip() or f"{obj.name}_copy"
        for collection in obj.users_collection or [bpy.context.scene.collection]:
            collection.objects.link(duplicate)
        return _object_summary(duplicate, detailed=True)

    def _set_visibility(self, name: str, visible: bool, render_visible: bool) -> dict[str, Any]:
        obj = _get_object(name)
        obj.hide_viewport = not visible
        obj.hide_render = not render_visible
        return {"name": obj.name, "visible": visible, "render_visible": render_visible}

    def _transform_object(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        obj = _get_object(name)
        obj.location = _vector(params["location"], "location")
        obj.rotation_euler = _vector(params["rotation"], "rotation")
        obj.scale = _vector(params["scale"], "scale")
        return _object_summary(obj, detailed=True)

    def _set_material(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        obj = _get_object(name)
        material_name = params["material_name"].strip() or f"{obj.name}_Material"
        material = _ensure_material(material_name, params["rgba"])
        if obj.data and hasattr(obj.data, "materials"):
            if obj.data.materials:
                obj.data.materials[0] = material
            else:
                obj.data.materials.append(material)
        return {"object": obj.name, "material": material.name, "rgba": list(material.diffuse_color)}

    def _add_modifier(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        obj = _get_object(name)
        modifier_name = params["name"].strip() or params["modifier_type"].title()
        modifier = obj.modifiers.new(modifier_name, params["modifier_type"])
        return {"object": obj.name, "modifier": modifier.name, "type": modifier.type}

    def _apply_modifier(self, name: str, modifier_name: str) -> dict[str, Any]:
        obj = _get_object(name)
        if obj.modifiers.get(modifier_name) is None:
            raise ValueError(f"Modifier not found on {name}: {modifier_name}")
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.modifier_apply(modifier=modifier_name)
        return {"object": obj.name, "applied_modifier": modifier_name}

    def _keyframe_transform(self, name: str, frame: int) -> dict[str, Any]:
        obj = _get_object(name)
        bpy.context.scene.frame_set(int(frame))
        for data_path in ("location", "rotation_euler", "scale"):
            obj.keyframe_insert(data_path=data_path, frame=int(frame))
        return {"object": obj.name, "frame": int(frame), "keyframes": ["location", "rotation_euler", "scale"]}

    def _delete_object(self, name: str) -> dict[str, Any]:
        obj = _get_object(name)
        bpy.data.objects.remove(obj, do_unlink=True)
        return {"deleted": name}

    def _create_material(self, params: dict[str, Any]) -> dict[str, Any]:
        material = _ensure_material(params["name"], params["rgba"])
        return _material_summary(material)

    def _update_material(self, material_name: str, params: dict[str, Any]) -> dict[str, Any]:
        material = bpy.data.materials.get(material_name)
        if material is None:
            raise ValueError(f"Material not found: {material_name}")
        material.diffuse_color = _rgba(params["rgba"])
        return _material_summary(material)

    def _set_active_camera(self, camera_name: str) -> dict[str, Any]:
        obj = _get_object(camera_name)
        if obj.type != "CAMERA":
            raise ValueError(f"Object is not a camera: {camera_name}")
        bpy.context.scene.camera = obj
        return {"active_camera": obj.name}

    def _update_light(self, light_name: str, params: dict[str, Any]) -> dict[str, Any]:
        obj = _get_object(light_name)
        if obj.type != "LIGHT":
            raise ValueError(f"Object is not a light: {light_name}")
        obj.data.energy = float(params["energy"])
        obj.data.color = _rgb(params["color"])
        return _object_summary(obj, detailed=True)

    def _set_frame(self, frame: int) -> dict[str, Any]:
        bpy.context.scene.frame_set(int(frame))
        return {"frame": bpy.context.scene.frame_current}

    def _set_frame_range(self, frame_start: int, frame_end: int) -> dict[str, Any]:
        start = int(frame_start)
        end = int(frame_end)
        if start > end:
            raise ValueError("frame_start must be less than or equal to frame_end")
        bpy.context.scene.frame_start = start
        bpy.context.scene.frame_end = end
        return {"frame_start": start, "frame_end": end}

    def _set_render_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        render = bpy.context.scene.render
        engine = params["engine"].strip()
        if engine:
            render.engine = engine
        render.resolution_x = max(1, int(params["resolution_x"]))
        render.resolution_y = max(1, int(params["resolution_y"]))
        render.fps = max(1, int(params["fps"]))
        return {
            "engine": render.engine,
            "resolution_x": render.resolution_x,
            "resolution_y": render.resolution_y,
            "fps": render.fps,
        }

    def _render_still(self, filepath: str) -> dict[str, Any]:
        if filepath.strip():
            path = Path(filepath).expanduser()
        else:
            handle = tempfile.NamedTemporaryFile(prefix="blender-slop-render-", suffix=".png", delete=False)
            path = Path(handle.name)
            handle.close()
        previous_path = bpy.context.scene.render.filepath
        try:
            bpy.context.scene.render.filepath = str(path)
            bpy.ops.render.render(write_still=True)
        finally:
            bpy.context.scene.render.filepath = previous_path
        return {"filepath": str(path)}

    def _set_world_color(self, rgba: list[Any]) -> dict[str, Any]:
        if bpy.context.scene.world is None:
            bpy.context.scene.world = bpy.data.worlds.new("World")
        color = _rgba(rgba)
        bpy.context.scene.world.color = color[:3]
        return {"world": bpy.context.scene.world.name, "color": _rounded_vector(bpy.context.scene.world.color)}


def _object_summary(obj: Any, *, detailed: bool = False) -> dict[str, Any]:
    data = {
        "name": obj.name,
        "type": obj.type,
        "location": _rounded_vector(obj.location),
        "rotation": _rounded_vector(obj.rotation_euler),
        "scale": _rounded_vector(obj.scale),
        "dimensions": _rounded_vector(obj.dimensions),
        "visible": obj.visible_get(),
        "selected": obj.select_get(),
        "materials": [slot.material.name for slot in obj.material_slots if slot.material],
        "modifiers": [{"name": modifier.name, "type": modifier.type} for modifier in obj.modifiers],
    }
    if obj.type == "CAMERA":
        data["lens"] = round(float(obj.data.lens), 4)
        data["sensor_width"] = round(float(obj.data.sensor_width), 4)
    elif obj.type == "LIGHT":
        data["light_type"] = obj.data.type
        data["energy"] = round(float(obj.data.energy), 4)
        data["color"] = _rounded_vector(obj.data.color)
    if detailed:
        data["world_bounding_box"] = _world_bbox(obj)
        if obj.type == "MESH" and obj.data:
            data["mesh"] = {
                "vertices": len(obj.data.vertices),
                "edges": len(obj.data.edges),
                "polygons": len(obj.data.polygons),
            }
    return data


def _material_summary(material: Any) -> dict[str, Any]:
    return {
        "name": material.name,
        "diffuse_color": [round(float(value), 4) for value in material.diffuse_color],
        "use_nodes": bool(material.use_nodes),
    }


def _collection_summary(collection: Any) -> dict[str, Any]:
    return {
        "name": collection.name,
        "object_count": len(collection.objects),
        "child_count": len(collection.children),
        "objects": [obj.name for obj in list(collection.objects)[:25]],
    }


def _ensure_material(name: str, rgba: list[Any]) -> Any:
    clean = name.strip()
    if not clean:
        raise ValueError("Material name is required")
    material = bpy.data.materials.get(clean) or bpy.data.materials.new(clean)
    material.diffuse_color = _rgba(rgba)
    return material


def _world_bbox(obj: Any) -> list[list[float]] | None:
    if not hasattr(obj, "bound_box") or not obj.bound_box:
        return None
    corners = [obj.matrix_world @ mathutils.Vector(corner) for corner in obj.bound_box]
    return [
        [round(min(getattr(corner, axis) for corner in corners), 4) for axis in ("x", "y", "z")],
        [round(max(getattr(corner, axis) for corner in corners), 4) for axis in ("x", "y", "z")],
    ]


def _get_object(name: str) -> Any:
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    return obj


def _vector(values: list[Any], name: str) -> Any:
    if len(values) != 3:
        raise ValueError(f"{name} must contain exactly 3 numbers")
    floats = [float(item) for item in values]
    if any(not math.isfinite(item) for item in floats):
        raise ValueError(f"{name} contains a non-finite number")
    return mathutils.Vector(floats)


def _rgba(values: list[Any]) -> tuple[float, float, float, float]:
    if len(values) != 4:
        raise ValueError("rgba must contain exactly 4 numbers")
    rgba = tuple(max(0.0, min(1.0, float(item))) for item in values)
    return rgba


def _rgb(values: list[Any]) -> tuple[float, float, float]:
    if len(values) != 3:
        raise ValueError("color must contain exactly 3 numbers")
    return tuple(max(0.0, min(1.0, float(item))) for item in values)


def _rounded_vector(values: Any) -> list[float]:
    return [round(float(item), 4) for item in values]


def _ensure_timer() -> None:
    if bpy is None:
        return
    if not bpy.app.timers.is_registered(_timer_tick):
        bpy.app.timers.register(_timer_tick, first_interval=0.05, persistent=True)


def _timer_tick() -> float | None:
    if _runtime is None or not _runtime.running:
        return None
    _runtime.tick()
    _set_status(_runtime.status_text)
    return 0.05


def _set_status(status: str) -> None:
    if bpy is not None and hasattr(bpy.types.Scene, "blender_slop_status"):
        bpy.context.scene.blender_slop_status = status


if bpy is not None:

    class BLENDER_SLOP_OT_install_dependencies(bpy.types.Operator):
        bl_idname = "blender_slop.install_dependencies"
        bl_label = "Install Dependencies"
        bl_description = "Install the published slop-ai SDK into Blender's Python environment"

        def execute(self, context: Any) -> set[str]:
            try:
                subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
                subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", DEPENDENCY_SPEC])
            except subprocess.CalledProcessError as exc:
                self.report({"ERROR"}, f"Dependency install failed: {exc}")
                return {"CANCELLED"}
            self.report({"INFO"}, "Installed slop-ai. Restart Blender or re-enable the add-on if import still fails.")
            return {"FINISHED"}


    class BLENDER_SLOP_OT_start(bpy.types.Operator):
        bl_idname = "blender_slop.start"
        bl_label = "Start SLOP Provider"

        def execute(self, context: Any) -> set[str]:
            global _runtime
            if _runtime and _runtime.running:
                self.report({"INFO"}, "Blender SLOP is already running")
                return {"FINISHED"}
            scene = context.scene
            _runtime = BlenderSlopRuntime(
                host=scene.blender_slop_host,
                port=scene.blender_slop_port,
                path=scene.blender_slop_path,
                refresh_interval=scene.blender_slop_refresh_interval,
                allowed_origins=scene.blender_slop_allowed_origins,
            )
            try:
                _runtime.start()
            except Exception as exc:
                _runtime = None
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            _set_status(_runtime.status_text)
            self.report({"INFO"}, _runtime.status_text)
            return {"FINISHED"}


    class BLENDER_SLOP_OT_stop(bpy.types.Operator):
        bl_idname = "blender_slop.stop"
        bl_label = "Stop SLOP Provider"

        def execute(self, context: Any) -> set[str]:
            global _runtime
            if _runtime is not None:
                _runtime.stop()
                _runtime = None
            _set_status("Stopped")
            return {"FINISHED"}


    class BLENDER_SLOP_OT_refresh(bpy.types.Operator):
        bl_idname = "blender_slop.refresh"
        bl_label = "Refresh SLOP Snapshot"

        def execute(self, context: Any) -> set[str]:
            if _runtime is None or not _runtime.running:
                self.report({"ERROR"}, "Blender SLOP is not running")
                return {"CANCELLED"}
            _runtime.refresh_snapshot(push=True)
            self.report({"INFO"}, "SLOP snapshot refreshed")
            return {"FINISHED"}


    class BLENDER_SLOP_PT_panel(bpy.types.Panel):
        bl_label = "Blender SLOP"
        bl_idname = "BLENDER_SLOP_PT_panel"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Blender SLOP"

        def draw(self, context: Any) -> None:
            layout = self.layout
            scene = context.scene
            layout.prop(scene, "blender_slop_host")
            layout.prop(scene, "blender_slop_port")
            layout.prop(scene, "blender_slop_path")
            layout.prop(scene, "blender_slop_allowed_origins")
            layout.prop(scene, "blender_slop_refresh_interval")
            layout.label(text=scene.blender_slop_status)
            row = layout.row(align=True)
            row.operator("blender_slop.start", icon="PLAY")
            row.operator("blender_slop.stop", icon="PAUSE")
            layout.operator("blender_slop.refresh", icon="FILE_REFRESH")
            layout.operator("blender_slop.install_dependencies", icon="IMPORT")


    _classes = (
        BLENDER_SLOP_OT_install_dependencies,
        BLENDER_SLOP_OT_start,
        BLENDER_SLOP_OT_stop,
        BLENDER_SLOP_OT_refresh,
        BLENDER_SLOP_PT_panel,
    )


    def register() -> None:
        for cls in _classes:
            bpy.utils.register_class(cls)
        bpy.types.Scene.blender_slop_host = bpy.props.StringProperty(name="Host", default="127.0.0.1")
        bpy.types.Scene.blender_slop_port = bpy.props.IntProperty(name="Port", default=8765, min=1, max=65535)
        bpy.types.Scene.blender_slop_path = bpy.props.StringProperty(name="Path", default="/slop")
        bpy.types.Scene.blender_slop_allowed_origins = bpy.props.StringProperty(
            name="Allowed Origins",
            description="Comma-separated browser origins allowed by the slop-ai WebSocket transport",
            default="",
        )
        bpy.types.Scene.blender_slop_refresh_interval = bpy.props.FloatProperty(
            name="Refresh Seconds",
            default=1.0,
            min=0.2,
            max=60.0,
        )
        bpy.types.Scene.blender_slop_status = bpy.props.StringProperty(name="Status", default="Stopped")


    def unregister() -> None:
        global _runtime
        if _runtime is not None:
            _runtime.stop()
            _runtime = None
        for attr in (
            "blender_slop_host",
            "blender_slop_port",
            "blender_slop_path",
            "blender_slop_allowed_origins",
            "blender_slop_refresh_interval",
            "blender_slop_status",
        ):
            if hasattr(bpy.types.Scene, attr):
                delattr(bpy.types.Scene, attr)
        for cls in reversed(_classes):
            bpy.utils.unregister_class(cls)

else:

    def register() -> None:
        raise RuntimeError("Blender SLOP add-on can only be registered inside Blender")


    def unregister() -> None:
        return None
