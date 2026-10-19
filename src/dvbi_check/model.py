"""Gemeinsames Datenmodell für Befunde und Prüfergebnisse."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Severity(IntEnum):
    INFO = 1
    WARNING = 2
    ERROR = 3

    @property
    def label(self) -> str:
        return {1: "Info", 2: "Warnung", 3: "Fehler"}[int(self)]


@dataclass
class Finding:
    module: str
    code: str
    severity: Severity
    message: str
    ref: str = ""
    service: str | None = None   # UniqueIdentifier des betroffenen Service
    subject: str = ""            # Kurzbezeichnung: Service-Name, URL, LCN-Tabelle …
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "code": self.code,
            "severity": self.severity.name,
            "message": self.message,
            "ref": self.ref,
            "service": self.service,
            "subject": self.subject,
            "detail": self.detail,
        }


class Findings(list):
    """Liste von Befunden eines Moduls mit bequemer add()-Methode."""

    def __init__(self, module: str):
        super().__init__()
        self.module = module

    def add(self, code: str, severity: Severity, message: str, *, ref: str = "",
            service: str | None = None, subject: str = "", detail: str = "") -> Finding:
        f = Finding(self.module, code, severity, message, ref, service, subject, detail)
        self.append(f)
        return f

    def error(self, code, message, **kw):
        return self.add(code, Severity.ERROR, message, **kw)

    def warning(self, code, message, **kw):
        return self.add(code, Severity.WARNING, message, **kw)

    def info(self, code, message, **kw):
        return self.add(code, Severity.INFO, message, **kw)


MODULE_TITLES = {
    "schema": "Schema-Validierung",
    "a177": "Semantik nach A177r6",
    "profile": "DVB-I Book Germany & DTVP-Profil",
    "cg": "Content Guide & Now/Next (TV-Anytime)",
    "images": "Logo- und Image-Endpunkte",
}


@dataclass
class ModuleResult:
    name: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    skipped: str | None = None
    duration: float = 0.0

    @property
    def title(self) -> str:
        return MODULE_TITLES.get(self.name, self.name)

    def count(self, sev: Severity) -> int:
        return sum(1 for f in self.findings if f.severity == sev)

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        if self.count(Severity.ERROR):
            return "error"
        if self.count(Severity.WARNING):
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "skipped": self.skipped,
            "duration_s": round(self.duration, 2),
            "counts": {s.name: self.count(s) for s in Severity},
            "stats": self.stats,
            "findings": [f.to_dict() for f in self.findings],
        }


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self.t0
