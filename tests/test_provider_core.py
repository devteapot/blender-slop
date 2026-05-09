from __future__ import annotations

from blender_slop_addon.provider_core import build_workspace_descriptor, jsonable, stable_id


def noop(_params):
    return {"ok": True}


def object_actions(_name):
    return {"inspect": {"handler": noop, "label": "Inspect"}}


def test_stable_id_is_slop_safe() -> None:
    node_id = stable_id("object", "Cube/With Weird~Chars")
    assert node_id.startswith("object_")
    assert "/" not in node_id
    assert "~" not in node_id


def test_jsonable_converts_unknown_values() -> None:
    class Thing:
        def __str__(self) -> str:
            return "thing"

    assert jsonable({"x": (1, Thing())}) == {"x": [1, "thing"]}


def test_workspace_descriptor_contains_native_affordances() -> None:
    snapshot = {
        "running": True,
        "url": "ws://127.0.0.1:8765/slop",
        "refresh_interval": 1.0,
        "scene": {
            "name": "Scene",
            "frame": 1,
            "object_count": 1,
            "materials_count": 0,
            "selected_objects": ["Cube"],
            "active_object": "Cube",
            "objects": [
                {
                    "name": "Cube",
                    "type": "MESH",
                    "location": [0, 0, 0],
                    "rotation": [0, 0, 0],
                    "scale": [1, 1, 1],
                    "dimensions": [2, 2, 2],
                    "visible": True,
                    "selected": True,
                    "materials": [],
                }
            ],
            "materials": [],
        },
        "last_result": None,
        "last_error": None,
    }
    descriptor = build_workspace_descriptor(
        snapshot,
        {
            "refresh": noop,
            "execute_python": noop,
            "capture_viewport": noop,
            "create_primitive": noop,
        },
        object_actions,
    )

    scene = descriptor["children"]["scene"]
    objects = scene["children"]["objects"]
    assert descriptor["props"]["status"] == "running"
    assert scene["actions"]["execute_python"]["dangerous"] is True
    assert objects["actions"]["create_primitive"]["params"]["primitive"]["enum"]
    assert objects["items"][0]["props"]["name"] == "Cube"
