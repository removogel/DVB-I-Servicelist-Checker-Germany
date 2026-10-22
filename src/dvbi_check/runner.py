"""Orchestration: load the list, run the modules, assemble the result."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import __version__
from .checks import ORDER, Context, get_runner
from .config import Config
from .httpclient import HttpClient
from .loader import load
from .model import ModuleResult, Severity
from .resources import ResourceError, Resources, locate
from .sl import ServiceListDoc

log = logging.getLogger("dvbi_check")


@dataclass
class RunResult:
    source: str
    started: datetime
    finished: datetime | None = None
    cfg: Config | None = None
    doc: ServiceListDoc | None = None
    resources: Resources | None = None
    modules: list[ModuleResult] = field(default_factory=list)
    fatal: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def findings(self):
        return [f for m in self.modules for f in m.findings]

    def count(self, sev: Severity) -> int:
        return sum(m.count(sev) for m in self.modules)

    def module(self, name: str) -> ModuleResult | None:
        return next((m for m in self.modules if m.name == name), None)


def run(location: str, cfg: Config, modules: list[str] | None = None, resources_dir: str | None = None,
        offline: bool = False, allow_download: bool = True) -> RunResult:
    rr = RunResult(location, datetime.now(timezone.utc), cfg=cfg)
    client = HttpClient(cfg.user_agent, cfg.timeout, cfg.retries)
    try:
        src = load(location, client, cfg.postcode)
    except Exception as e:  # noqa: BLE001
        rr.fatal = f"Service list could not be loaded: {e}"
        rr.finished = datetime.now(timezone.utc)
        return rr
    rr.meta["source"] = {"location": src.location, "request_url": src.request_url, **src.meta}
    try:
        doc = ServiceListDoc(src.data, src.location)
    except Exception as e:  # noqa: BLE001
        rr.fatal = f"Service list could not be parsed: {e}"
        rr.finished = datetime.now(timezone.utc)
        return rr
    rr.doc = doc
    rr.meta["list"] = {"id": doc.root.get("id"), "version": doc.root.get("version"), "name": doc.name,
                       "provider": doc.provider, "namespace": doc.ns, "revision": doc.revision,
                       "responseStatus": doc.root.get("responseStatus"), "services": len(doc.services)}
    try:
        rr.resources = locate(resources_dir, allow_download=allow_download and not offline)
    except ResourceError as e:
        log.warning("%s", e)
        rr.meta["resources_error"] = str(e)
    ctx = Context(doc, src, cfg, client, rr.resources, offline)
    for name in [m for m in ORDER if not modules or m in modules]:
        log.info("Checking %s …", name)
        try:
            rr.modules.append(get_runner(name)(ctx))
        except Exception as e:  # noqa: BLE001
            log.exception("Module %s aborted", name)
            rr.modules.append(ModuleResult(name, skipped=f"Aborted: {type(e).__name__}: {e}"))
    rr.meta["http_requests"] = client.request_count
    rr.meta["tool_version"] = __version__
    rr.meta["resources"] = {"path": str(rr.resources.root), "ref": rr.resources.ref} if rr.resources else None
    rr.finished = datetime.now(timezone.utc)
    return rr
