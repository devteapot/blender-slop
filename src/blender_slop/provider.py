"""SLOP state projection and affordances for Blender."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from slop_ai import SlopServer

from .connection import DEFAULT_HOST, DEFAULT_PORT, BlenderConnection


class BlenderClient(Protocol):
    host: str
    port: int

    @property
    def connected(self) -> bool: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def send_command(self, command_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass
class ProviderSnapshot:
    connected: bool = False
    scene: dict[str, Any] = field(default_factory=dict)
    statuses: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_action: str | None = None
    last_result: Any = None
    last_error: str | None = None
    screenshot_path: str | None = None


def create_slop_server(
    *,
    host: str | None = None,
    port: int | None = None,
    connect_on_start: bool = True,
) -> SlopServer:
    """Create the configured SLOP server instance used by the CLI."""
    provider = BlenderSlopProvider(
        host=host or os.getenv("BLENDER_HOST", DEFAULT_HOST),
        port=port or int(os.getenv("BLENDER_PORT", str(DEFAULT_PORT))),
        connect_on_start=connect_on_start,
    )
    return provider.slop


class BlenderSlopProvider:
    """Builds a SLOP provider around the BlenderMCP add-on protocol."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        connection: BlenderClient | None = None,
        connect_on_start: bool = True,
    ) -> None:
        self.connection = connection or BlenderConnection(host=host, port=port)
        self.snapshot = ProviderSnapshot()
        self.slop = SlopServer("blender", "Blender")

        @self.slop.node("workspace")
        def workspace_node() -> dict[str, Any]:
            return self._workspace_descriptor()

        if connect_on_start:
            self.refresh_snapshot()

    def refresh_snapshot(self) -> dict[str, Any]:
        """Refresh cached Blender state without exposing exceptions to SLOP rebuilds."""
        try:
            self.connection.connect()
            scene = self.connection.send_command("get_scene_info")
            self.snapshot.scene = scene if isinstance(scene, dict) else {"value": scene}
            self.snapshot.statuses = self._load_statuses()
            self.snapshot.connected = True
            self.snapshot.last_error = None
        except Exception as exc:
            self.snapshot.connected = False
            self.snapshot.last_error = str(exc)
        self.slop.refresh()
        return {
            "connected": self.snapshot.connected,
            "error": self.snapshot.last_error,
        }

    def _load_statuses(self) -> dict[str, dict[str, Any]]:
        statuses: dict[str, dict[str, Any]] = {}
        for key, command in (
            ("polyhaven", "get_polyhaven_status"),
            ("sketchfab", "get_sketchfab_status"),
            ("hyper3d", "get_hyper3d_status"),
            ("hunyuan3d", "get_hunyuan3d_status"),
        ):
            try:
                result = self.connection.send_command(command)
                statuses[key] = result if isinstance(result, dict) else {"message": str(result)}
            except Exception as exc:
                statuses[key] = {"enabled": False, "message": str(exc), "error": str(exc)}
        return statuses

    def _workspace_descriptor(self) -> dict[str, Any]:
        scene = self.snapshot.scene or {}
        object_count = scene.get("object_count", 0)
        material_count = scene.get("materials_count", 0)
        status = "connected" if self.snapshot.connected else "disconnected"

        return {
            "type": "view",
            "props": {
                "label": "Blender SLOP",
                "status": status,
                "host": self.connection.host,
                "port": self.connection.port,
                "scene": scene.get("name"),
                "object_count": object_count,
                "materials_count": material_count,
            },
            "summary": self._workspace_summary(status, object_count, material_count),
            "meta": {"salience": 1.0, "pinned": True},
            "actions": {
                "refresh": {
                    "handler": lambda _params: self._record_action("refresh", self.refresh_snapshot),
                    "label": "Refresh",
                    "description": "Reconnect if necessary and refresh Blender scene and integration state.",
                    "idempotent": True,
                    "estimate": "slow",
                },
            },
            "children": {
                "connection": self._connection_descriptor(),
                "scene": self._scene_descriptor(),
                "integrations": self._integrations_descriptor(),
                "commands": self._commands_descriptor(),
                "last_result": self._last_result_descriptor(),
            },
        }

    def _workspace_summary(self, status: str, object_count: int, material_count: int) -> str:
        if self.snapshot.last_error:
            return f"Blender is {status}: {self.snapshot.last_error}"
        return f"Blender is {status}; {object_count} objects and {material_count} materials visible."

    def _connection_descriptor(self) -> dict[str, Any]:
        return {
            "type": "status",
            "props": {
                "label": "Connection",
                "connected": self.snapshot.connected,
                "host": self.connection.host,
                "port": self.connection.port,
                "last_error": self.snapshot.last_error,
            },
            "summary": self.snapshot.last_error or "Blender socket connection state.",
            "meta": {"salience": 0.9},
            "actions": {
                "reconnect": {
                    "handler": lambda _params: self._record_action("reconnect", self.refresh_snapshot),
                    "label": "Reconnect",
                    "description": "Open or re-open the socket connection to the BlenderMCP add-on.",
                    "idempotent": True,
                    "estimate": "slow",
                },
                "disconnect": {
                    "handler": lambda _params: self._record_action("disconnect", self._disconnect),
                    "label": "Disconnect",
                    "description": "Close the current Blender socket connection.",
                    "idempotent": True,
                    "estimate": "instant",
                },
            },
        }

    def _scene_descriptor(self) -> dict[str, Any]:
        scene = self.snapshot.scene or {}
        objects = scene.get("objects") if isinstance(scene.get("objects"), list) else []
        object_items = [self._object_item_descriptor(obj) for obj in objects if isinstance(obj, dict)]

        return {
            "type": "view",
            "props": {
                "label": scene.get("name") or "Scene",
                "name": scene.get("name"),
                "object_count": scene.get("object_count", len(objects)),
                "materials_count": scene.get("materials_count"),
            },
            "summary": "Current Blender scene snapshot.",
            "meta": {"salience": 0.95},
            "actions": {
                "refresh": {
                    "handler": lambda _params: self._record_action("scene.refresh", self.refresh_snapshot),
                    "label": "Refresh Scene",
                    "description": "Refresh scene objects, counts, and integration state.",
                    "idempotent": True,
                    "estimate": "slow",
                },
                "execute_python": {
                    "handler": self._execute_python,
                    "label": "Execute Python",
                    "description": "Run arbitrary Python inside Blender through the BlenderMCP add-on.",
                    "dangerous": True,
                    "estimate": "slow",
                    "params": {
                        "code": {"type": "string", "description": "Python code to execute in Blender."},
                    },
                },
                "capture_viewport": {
                    "handler": self._capture_viewport,
                    "label": "Capture Viewport",
                    "description": "Capture the active Blender viewport to a temporary image file.",
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
                    "summary": f"Showing {len(object_items)} objects from Blender's scene summary.",
                    "meta": {
                        "salience": 0.85,
                        "total_children": scene.get("object_count", len(object_items)),
                        "window": (0, len(object_items)),
                    },
                    "items": object_items,
                },
            },
        }

    def _object_item_descriptor(self, obj: dict[str, Any]) -> dict[str, Any]:
        name = str(obj.get("name", "Object"))
        return {
            "id": _stable_id("object", name),
            "props": {
                "label": name,
                "name": name,
                "object_type": obj.get("type"),
                "location": obj.get("location"),
            },
            "summary": f"{obj.get('type', 'Object')} at {obj.get('location', 'unknown location')}",
            "meta": {"salience": 0.75},
            "actions": {
                "inspect": {
                    "handler": lambda _params, object_name=name: self._inspect_object(object_name),
                    "label": "Inspect",
                    "description": "Fetch detailed object properties, materials, mesh counts, and bounding box.",
                    "idempotent": True,
                    "estimate": "slow",
                },
            },
        }

    def _integrations_descriptor(self) -> dict[str, Any]:
        return {
            "type": "group",
            "props": {"label": "Integrations"},
            "summary": "Optional asset and generation integrations exposed by the BlenderMCP add-on.",
            "meta": {"salience": 0.7},
            "children": {
                "polyhaven": self._polyhaven_descriptor(),
                "sketchfab": self._sketchfab_descriptor(),
                "hyper3d": self._hyper3d_descriptor(),
                "hunyuan3d": self._hunyuan3d_descriptor(),
            },
        }

    def _polyhaven_descriptor(self) -> dict[str, Any]:
        status = self.snapshot.statuses.get("polyhaven", {})
        return {
            "type": "status",
            "props": {
                "label": "Poly Haven",
                "enabled": bool(status.get("enabled")),
                "message": status.get("message"),
            },
            "summary": status.get("message") or "Poly Haven asset search and download.",
            "actions": {
                "get_categories": {
                    "handler": self._polyhaven_categories,
                    "label": "Get Categories",
                    "description": "List Poly Haven categories for hdris, textures, models, or all.",
                    "idempotent": True,
                    "estimate": "slow",
                    "params": {"asset_type": {"type": "string", "enum": ["hdris", "textures", "models", "all"]}},
                },
                "search_assets": {
                    "handler": self._polyhaven_search,
                    "label": "Search Assets",
                    "description": "Search Poly Haven assets. Use an empty categories string for no category filter.",
                    "idempotent": True,
                    "estimate": "slow",
                    "params": {
                        "asset_type": {"type": "string", "enum": ["hdris", "textures", "models", "all"]},
                        "categories": {"type": "string", "description": "Comma-separated categories or empty string."},
                    },
                },
                "download_asset": {
                    "handler": self._polyhaven_download,
                    "label": "Download Asset",
                    "description": "Download and import a Poly Haven HDRI, texture, or model.",
                    "dangerous": True,
                    "estimate": "slow",
                    "params": {
                        "asset_id": "string",
                        "asset_type": {"type": "string", "enum": ["hdris", "textures", "models"]},
                        "resolution": "string",
                        "file_format": {"type": "string", "description": "Format such as hdr, exr, jpg, png, gltf, fbx, or empty string."},
                    },
                },
                "set_texture": {
                    "handler": self._polyhaven_set_texture,
                    "label": "Set Texture",
                    "description": "Apply a previously downloaded Poly Haven texture to an object.",
                    "dangerous": True,
                    "estimate": "slow",
                    "params": {"object_name": "string", "texture_id": "string"},
                },
            },
        }

    def _sketchfab_descriptor(self) -> dict[str, Any]:
        status = self.snapshot.statuses.get("sketchfab", {})
        return {
            "type": "status",
            "props": {
                "label": "Sketchfab",
                "enabled": bool(status.get("enabled")),
                "message": status.get("message"),
            },
            "summary": status.get("message") or "Sketchfab model search and import.",
            "actions": {
                "search_models": {
                    "handler": self._sketchfab_search,
                    "label": "Search Models",
                    "description": "Search Sketchfab for models. Use an empty categories string for no category filter.",
                    "idempotent": True,
                    "estimate": "slow",
                    "params": {
                        "query": "string",
                        "categories": {"type": "string", "description": "Comma-separated categories or empty string."},
                        "count": "integer",
                        "downloadable": "boolean",
                    },
                },
                "preview_model": {
                    "handler": self._sketchfab_preview,
                    "label": "Preview Model",
                    "description": "Fetch a Sketchfab preview thumbnail into a temporary image file.",
                    "idempotent": True,
                    "estimate": "slow",
                    "params": {"uid": "string"},
                },
                "download_model": {
                    "handler": self._sketchfab_download,
                    "label": "Download Model",
                    "description": "Download and import a Sketchfab model by UID.",
                    "dangerous": True,
                    "estimate": "slow",
                    "params": {"uid": "string", "target_size": "number"},
                },
            },
        }

    def _hyper3d_descriptor(self) -> dict[str, Any]:
        status = self.snapshot.statuses.get("hyper3d", {})
        return {
            "type": "status",
            "props": {
                "label": "Hyper3D Rodin",
                "enabled": bool(status.get("enabled")),
                "message": status.get("message"),
            },
            "summary": status.get("message") or "Hyper3D Rodin model generation.",
            "actions": {
                "generate_from_text": {
                    "handler": self._rodin_generate_text,
                    "label": "Generate From Text",
                    "description": "Start a Hyper3D Rodin model generation job from an English text prompt.",
                    "dangerous": True,
                    "estimate": "async",
                    "params": {
                        "text_prompt": "string",
                        "bbox_condition": {"type": "array", "items": {"type": "number"}},
                    },
                },
                "generate_from_images": {
                    "handler": self._rodin_generate_images,
                    "label": "Generate From Images",
                    "description": "Start a Hyper3D Rodin job from image paths or URLs. Pass one list empty.",
                    "dangerous": True,
                    "estimate": "async",
                    "params": {
                        "input_image_paths": {"type": "array", "items": {"type": "string"}},
                        "input_image_urls": {"type": "array", "items": {"type": "string"}},
                        "bbox_condition": {"type": "array", "items": {"type": "number"}},
                    },
                },
                "poll_job": {
                    "handler": self._rodin_poll,
                    "label": "Poll Job",
                    "description": "Poll a Hyper3D Rodin job. Pass one identifier and an empty string for the other.",
                    "idempotent": True,
                    "estimate": "slow",
                    "params": {"subscription_key": "string", "request_id": "string"},
                },
                "import_asset": {
                    "handler": self._rodin_import,
                    "label": "Import Asset",
                    "description": "Import a completed Hyper3D Rodin asset. Pass one identifier and an empty string for the other.",
                    "dangerous": True,
                    "estimate": "slow",
                    "params": {"name": "string", "task_uuid": "string", "request_id": "string"},
                },
            },
        }

    def _hunyuan3d_descriptor(self) -> dict[str, Any]:
        status = self.snapshot.statuses.get("hunyuan3d", {})
        return {
            "type": "status",
            "props": {
                "label": "Hunyuan3D",
                "enabled": bool(status.get("enabled")),
                "message": status.get("message"),
            },
            "summary": status.get("message") or "Hunyuan3D model generation.",
            "actions": {
                "generate": {
                    "handler": self._hunyuan_generate,
                    "label": "Generate",
                    "description": "Start a Hunyuan3D generation job. Pass an empty string for unused prompt or image URL.",
                    "dangerous": True,
                    "estimate": "async",
                    "params": {"text_prompt": "string", "input_image_url": "string"},
                },
                "poll_job": {
                    "handler": self._hunyuan_poll,
                    "label": "Poll Job",
                    "description": "Poll a Hunyuan3D generation job.",
                    "idempotent": True,
                    "estimate": "slow",
                    "params": {"job_id": "string"},
                },
                "import_asset": {
                    "handler": self._hunyuan_import,
                    "label": "Import Asset",
                    "description": "Import a completed Hunyuan3D asset ZIP URL.",
                    "dangerous": True,
                    "estimate": "slow",
                    "params": {"name": "string", "zip_file_url": "string"},
                },
            },
        }

    def _commands_descriptor(self) -> dict[str, Any]:
        return {
            "type": "group",
            "props": {"label": "Raw Commands"},
            "summary": "Escape hatch for BlenderMCP add-on commands not projected as first-class affordances.",
            "meta": {"salience": 0.35},
            "actions": {
                "send": {
                    "handler": self._send_raw_command,
                    "label": "Send Command",
                    "description": "Send a raw BlenderMCP command. params_json must be a JSON object string.",
                    "dangerous": True,
                    "estimate": "slow",
                    "params": {"command_type": "string", "params_json": "string"},
                },
            },
        }

    def _last_result_descriptor(self) -> dict[str, Any]:
        return {
            "type": "document",
            "props": {
                "label": "Last Result",
                "action": self.snapshot.last_action,
                "result": _compact_value(self.snapshot.last_result),
                "error": self.snapshot.last_error,
                "screenshot_path": self.snapshot.screenshot_path,
            },
            "summary": _result_summary(self.snapshot.last_action, self.snapshot.last_result, self.snapshot.last_error),
            "meta": {"salience": 0.65 if self.snapshot.last_action else 0.25},
        }

    def _disconnect(self) -> dict[str, Any]:
        self.connection.disconnect()
        self.snapshot.connected = False
        return {"connected": False}

    def _record_action(self, name: str, callback: Any) -> dict[str, Any]:
        try:
            result = callback()
            self.snapshot.last_action = name
            self.snapshot.last_result = result
            self.snapshot.last_error = None
            return {"result": _compact_value(result)}
        except Exception as exc:
            self.snapshot.connected = False
            self.snapshot.last_action = name
            self.snapshot.last_error = str(exc)
            raise

    def _command_action(self, name: str, command_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            result = self.connection.send_command(command_type, params or {})
            if command_type not in {"get_scene_info", "get_polyhaven_status", "get_sketchfab_status", "get_hyper3d_status", "get_hunyuan3d_status"}:
                self.refresh_snapshot()
            return result

        return self._record_action(name, run)

    def _inspect_object(self, object_name: str) -> dict[str, Any]:
        return self._command_action(f"object.inspect:{object_name}", "get_object_info", {"name": object_name})

    def _execute_python(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._command_action("scene.execute_python", "execute_code", {"code": params["code"]})

    def _capture_viewport(self, params: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            suffix = ".png"
            handle = tempfile.NamedTemporaryFile(prefix="blender-slop-viewport-", suffix=suffix, delete=False)
            path = handle.name
            handle.close()
            result = self.connection.send_command(
                "get_viewport_screenshot",
                {"max_size": params["max_size"], "filepath": path, "format": "png"},
            )
            self.snapshot.screenshot_path = path if Path(path).exists() else None
            return {"screenshot_path": self.snapshot.screenshot_path, **result}

        return self._record_action("scene.capture_viewport", run)

    def _polyhaven_categories(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._command_action("polyhaven.get_categories", "get_polyhaven_categories", params)

    def _polyhaven_search(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = dict(params)
        payload["categories"] = payload["categories"] or None
        return self._command_action("polyhaven.search_assets", "search_polyhaven_assets", payload)

    def _polyhaven_download(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = dict(params)
        payload["file_format"] = payload["file_format"] or None
        return self._command_action("polyhaven.download_asset", "download_polyhaven_asset", payload)

    def _polyhaven_set_texture(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._command_action("polyhaven.set_texture", "set_texture", params)

    def _sketchfab_search(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = dict(params)
        payload["categories"] = payload["categories"] or None
        return self._command_action("sketchfab.search_models", "search_sketchfab_models", payload)

    def _sketchfab_preview(self, params: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            result = self.connection.send_command("get_sketchfab_model_preview", {"uid": params["uid"]})
            image_data = result.get("image_data")
            if isinstance(image_data, str):
                image_format = result.get("format", "jpeg")
                suffix = ".jpg" if image_format in {"jpeg", "jpg"} else f".{image_format}"
                handle = tempfile.NamedTemporaryFile(prefix="blender-slop-sketchfab-", suffix=suffix, delete=False)
                handle.write(base64.b64decode(image_data))
                handle.close()
                result = {k: v for k, v in result.items() if k != "image_data"}
                result["preview_path"] = handle.name
            return result

        return self._record_action("sketchfab.preview_model", run)

    def _sketchfab_download(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._command_action(
            "sketchfab.download_model",
            "download_sketchfab_model",
            {"uid": params["uid"], "normalize_size": True, "target_size": params["target_size"]},
        )

    def _rodin_generate_text(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._command_action(
            "hyper3d.generate_from_text",
            "create_rodin_job",
            {
                "text_prompt": params["text_prompt"],
                "images": None,
                "bbox_condition": _process_bbox(params["bbox_condition"]),
            },
        )

    def _rodin_generate_images(self, params: dict[str, Any]) -> dict[str, Any]:
        paths = params["input_image_paths"]
        urls = params["input_image_urls"]
        if bool(paths) == bool(urls):
            raise ValueError("Provide either input_image_paths or input_image_urls, not both.")

        images: list[Any]
        if paths:
            images = []
            for path in paths:
                file_path = Path(path)
                if not file_path.exists():
                    raise ValueError(f"Image path does not exist: {path}")
                images.append((file_path.suffix, base64.b64encode(file_path.read_bytes()).decode("ascii")))
        else:
            for url in urls:
                parsed = urlparse(url)
                if not parsed.scheme or not parsed.netloc:
                    raise ValueError(f"Invalid image URL: {url}")
            images = list(urls)

        return self._command_action(
            "hyper3d.generate_from_images",
            "create_rodin_job",
            {"text_prompt": None, "images": images, "bbox_condition": _process_bbox(params["bbox_condition"])},
        )

    def _rodin_poll(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = _one_of_strings(params, "subscription_key", "request_id")
        return self._command_action("hyper3d.poll_job", "poll_rodin_job_status", payload)

    def _rodin_import(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = {"name": params["name"], **_one_of_strings(params, "task_uuid", "request_id")}
        return self._command_action("hyper3d.import_asset", "import_generated_asset", payload)

    def _hunyuan_generate(self, params: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "text_prompt": params["text_prompt"] or None,
            "image": params["input_image_url"] or None,
        }
        return self._command_action("hunyuan3d.generate", "create_hunyuan_job", payload)

    def _hunyuan_poll(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._command_action("hunyuan3d.poll_job", "poll_hunyuan_job_status", params)

    def _hunyuan_import(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._command_action("hunyuan3d.import_asset", "import_generated_asset_hunyuan", params)

    def _send_raw_command(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            command_params = json.loads(params["params_json"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"params_json must be valid JSON: {exc}") from exc
        if not isinstance(command_params, dict):
            raise ValueError("params_json must decode to a JSON object")
        return self._command_action(f"raw.{params['command_type']}", params["command_type"], command_params)


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-").lower()[:36]
    if not slug:
        slug = "item"
    return f"{prefix}_{digest}_{slug}"


def _compact_value(value: Any, *, max_string: int = 500, max_items: int = 12) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_string else value[: max_string - 3] + "..."
    if isinstance(value, dict):
        return {str(k): _compact_value(v) for k, v in list(value.items())[:max_items]}
    if isinstance(value, list):
        return [_compact_value(v) for v in value[:max_items]]
    return value


def _result_summary(action: str | None, result: Any, error: str | None) -> str:
    if error:
        return f"Last error from {action or 'provider'}: {error}"
    if action is None:
        return "No SLOP actions have been invoked yet."
    if isinstance(result, dict):
        keys = ", ".join(list(result.keys())[:6])
        return f"Last action {action} returned object keys: {keys}"
    return f"Last action {action} returned {type(result).__name__}."


def _process_bbox(original_bbox: list[float] | list[int] | None) -> list[int] | None:
    if not original_bbox:
        return None
    if len(original_bbox) != 3:
        raise ValueError("bbox_condition must contain exactly three numbers")
    if any(i <= 0 for i in original_bbox):
        raise ValueError("bbox_condition values must be greater than zero")
    if all(isinstance(i, int) for i in original_bbox):
        return list(original_bbox)
    scale = max(original_bbox)
    return [int(float(i) / scale * 100) for i in original_bbox]


def _one_of_strings(params: dict[str, Any], first: str, second: str) -> dict[str, str]:
    values = {first: params.get(first) or "", second: params.get(second) or ""}
    present = {key: value for key, value in values.items() if value}
    if len(present) != 1:
        raise ValueError(f"Provide exactly one of {first} or {second}")
    return present
