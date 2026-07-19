"""T-1 production dependency contract for Uvicorn WebSocket Upgrade support."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PACKAGES = {"websockets", "wsproto"}


def _dependency_name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip().lower()


def test_api_extra_declares_a_uvicorn_websocket_protocol() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    api_dependencies = project["project"]["optional-dependencies"]["api"]

    assert {_dependency_name(requirement) for requirement in api_dependencies} & PROTOCOL_PACKAGES


def test_lock_resolves_websocket_protocol_in_api_extra() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    veridex = next(package for package in lock["package"] if package["name"] == "veridex")
    api_dependencies = {dependency["name"] for dependency in veridex["optional-dependencies"]["api"]}

    assert api_dependencies & PROTOCOL_PACKAGES
