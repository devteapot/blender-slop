"""SLOP provider for Blender via the BlenderMCP add-on socket protocol."""

from .connection import BlenderConnection
from .provider import BlenderSlopProvider, create_slop_server

__all__ = ["BlenderConnection", "BlenderSlopProvider", "create_slop_server"]

__version__ = "0.1.0"
