from __future__ import annotations

import asyncio

from blender_slop import BlenderSlopProvider


class FakeConnection:
    host = "localhost"
    port = 9876

    def __init__(self) -> None:
        self._connected = False
        self.commands: list[tuple[str, dict]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def send_command(self, command_type: str, params: dict | None = None) -> dict:
        params = params or {}
        self.commands.append((command_type, params))
        if command_type == "get_scene_info":
            return {
                "name": "Scene",
                "object_count": 1,
                "materials_count": 2,
                "objects": [
                    {"name": "Cube", "type": "MESH", "location": [0, 0, 0]},
                ],
            }
        if command_type.endswith("_status") or command_type in {
            "get_polyhaven_status",
            "get_sketchfab_status",
            "get_hyper3d_status",
            "get_hunyuan3d_status",
        }:
            return {"enabled": False, "message": f"{command_type} disabled"}
        if command_type == "get_object_info":
            return {
                "name": params["name"],
                "type": "MESH",
                "location": [0, 0, 0],
                "materials": ["Mat"],
                "mesh": {"vertices": 8, "edges": 12, "polygons": 6},
            }
        if command_type == "execute_code":
            return {"result": "ok"}
        return {"ok": True}


class MockConsumer:
    def __init__(self) -> None:
        self.messages = []

    def send(self, message: dict) -> None:
        self.messages.append(message)

    def close(self) -> None:
        pass


def run(coro):
    return asyncio.run(coro)


def test_provider_projects_scene_state() -> None:
    provider = BlenderSlopProvider(connection=FakeConnection())
    tree = provider.slop.tree.to_dict()

    workspace = tree["children"][0]
    assert workspace["id"] == "workspace"
    assert workspace["properties"]["status"] == "connected"

    scene = next(child for child in workspace["children"] if child["id"] == "scene")
    objects = next(child for child in scene["children"] if child["id"] == "objects")
    assert objects["properties"]["count"] == 1
    assert objects["children"][0]["properties"]["name"] == "Cube"
    assert objects["children"][0]["affordances"][0]["action"] == "inspect"


def test_object_inspect_affordance_invokes_blender() -> None:
    fake = FakeConnection()
    provider = BlenderSlopProvider(connection=fake)
    conn = MockConsumer()
    provider.slop.handle_connection(conn)

    object_id = provider.slop.tree.children[0].children[1].children[0].children[0].id
    run(
        provider.slop.handle_message(
            conn,
            {
                "type": "invoke",
                "id": "inspect-1",
                "path": f"/blender/workspace/scene/objects/{object_id}",
                "action": "inspect",
                "params": {},
            },
        )
    )

    assert ("get_object_info", {"name": "Cube"}) in fake.commands
    result = [message for message in conn.messages if message.get("id") == "inspect-1"][0]
    assert result["type"] == "result"
    assert result["status"] == "ok"


def test_execute_python_requires_code_param() -> None:
    provider = BlenderSlopProvider(connection=FakeConnection())
    conn = MockConsumer()
    provider.slop.handle_connection(conn)

    run(
        provider.slop.handle_message(
            conn,
            {
                "type": "invoke",
                "id": "exec-1",
                "path": "/blender/workspace/scene",
                "action": "execute_python",
                "params": {},
            },
        )
    )

    result = [message for message in conn.messages if message.get("id") == "exec-1"][0]
    assert result["status"] == "error"
    assert result["error"]["code"] == "invalid_params"
