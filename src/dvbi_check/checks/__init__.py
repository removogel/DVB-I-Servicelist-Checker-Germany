"""Check modules. Each module exports run(ctx) -> ModuleResult."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..httpclient import HttpClient
from ..loader import Source
from ..resources import Resources
from ..sl import ServiceListDoc


@dataclass
class Context:
    doc: ServiceListDoc
    source: Source
    cfg: Config
    client: HttpClient
    res: Resources | None
    offline: bool = False


ORDER = ["schema", "a177", "profile", "cg", "images", "ait", "mpd"]


_MODULES = {
    "schema": "schema",
    "a177": "a177",
    "profile": "profile_de",
    "cg": "contentguide",
    "images": "images",
    "ait": "ait",
    "mpd": "mpd",
}


def get_runner(name: str):
    import importlib
    return importlib.import_module(f".{_MODULES[name]}", __name__).run
