"""Socket client for the BlenderMCP add-on.

The Blender side is intentionally left compatible with the upstream
``addon.py`` protocol: JSON command objects over a persistent TCP socket.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass
from typing import Any

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876
DEFAULT_TIMEOUT_SECONDS = 180.0

logger = logging.getLogger(__name__)


class BlenderConnectionError(RuntimeError):
    """Raised when Blender cannot be reached or returns an invalid response."""


@dataclass
class BlenderConnection:
    """Persistent JSON-over-TCP connection to the BlenderMCP add-on."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    sock: socket.socket | None = None

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self) -> None:
        """Open the socket if it is not already open."""
        if self.sock is not None:
            return

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
        except OSError as exc:
            raise BlenderConnectionError(
                f"Could not connect to Blender at {self.host}:{self.port}. "
                "Start Blender, enable the BlenderMCP add-on, and click Connect."
            ) from exc

        self.sock = sock
        logger.info("connected to Blender at %s:%s", self.host, self.port)

    def disconnect(self) -> None:
        """Close the current socket."""
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            logger.debug("error while closing Blender socket", exc_info=True)

    def send_command(self, command_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one BlenderMCP command and return its ``result`` object."""
        self.connect()
        assert self.sock is not None

        payload = {"type": command_type, "params": params or {}}
        try:
            self.sock.sendall(json.dumps(payload).encode("utf-8"))
            response = json.loads(self._receive_json().decode("utf-8"))
        except socket.timeout as exc:
            self.disconnect()
            raise BlenderConnectionError("Timed out waiting for Blender. Try a smaller operation.") from exc
        except (OSError, json.JSONDecodeError) as exc:
            self.disconnect()
            raise BlenderConnectionError(f"Invalid response from Blender: {exc}") from exc

        if response.get("status") == "error":
            raise BlenderConnectionError(response.get("message", "Unknown Blender error"))
        return response.get("result") or {}

    def _receive_json(self, buffer_size: int = 8192) -> bytes:
        """Read until a complete JSON value has been received."""
        assert self.sock is not None
        chunks: list[bytes] = []
        self.sock.settimeout(self.timeout)

        while True:
            chunk = self.sock.recv(buffer_size)
            if not chunk:
                if chunks:
                    break
                raise BlenderConnectionError("Connection closed before Blender returned data")

            chunks.append(chunk)
            data = b"".join(chunks)
            try:
                json.loads(data.decode("utf-8"))
                return data
            except json.JSONDecodeError:
                continue

        data = b"".join(chunks)
        json.loads(data.decode("utf-8"))
        return data
