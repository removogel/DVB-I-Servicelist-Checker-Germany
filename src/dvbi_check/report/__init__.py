"""Auswertung: HTML-Report, JSON und CSV."""
from __future__ import annotations

import csv
import json
from collections import OrderedDict, defaultdict
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, select_autoescape

from ..checks import ORDER
from ..model import MODULE_TITLES, Severity
from ..runner import RunResult

SEV_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def write_reports(rr: RunResult, out: Path, formats: list[str]) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    stamp = rr.started.astimezone(ZoneInfo("Europe/Berlin")).strftime("%Y%m%d-%H%M")
    base = out / f"dvbi-report-{stamp}"
    paths = []
    data = to_json(rr)
    if "json" in formats:
        p = base.with_suffix(".json")
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        paths.append(p)
    if "csv" in formats:
        p = base.with_suffix(".csv")
        with p.open("w", newline="", encoding="utf-8-sig") as fh:  # BOM für Excel
            w = csv.writer(fh, delimiter=";")
            w.writerow(["Modul", "Code", "Schwere", "Meldung", "Referenz", "Service-ID", "Betrifft", "Detail"])
            for fd in rr.findings:
                w.writerow([MODULE_TITLES.get(fd.module, fd.module), fd.code, fd.severity.label, fd.message,
                            fd.ref, fd.service or "", fd.subject, fd.detail])
        paths.append(p)
    if "html" in formats:
        p = base.with_suffix(".html")
        p.write_text(render_html(rr), encoding="utf-8")
        paths.append(p)
    return paths


def to_json(rr: RunResult) -> dict:
    return {
        "tool": rr.meta.get("tool_version"),
        "started": rr.started.isoformat(),
        "finished": rr.finished.isoformat() if rr.finished else None,
        "profile": rr.cfg.profile if rr.cfg else None,
        "meta": rr.meta,
        "summary": {s.name: rr.count(s) for s in Severity},
        "modules": [m.to_dict() for m in rr.modules],
    }


# --------------------------------------------------------------------------- view model
def _lcn_index(rr: RunResult) -> dict[str, list[int]]:
    """LCNs je Service aus der bundesweiten Referenztabelle (sonst alle Tabellen)."""
    doc = rr.doc
    from ..checks.profile_de import _reference_table  # noqa: PLC0415
    ref = _reference_table(doc)
    tables = [ref] if ref is not None else doc.lcn_tables
    idx = defaultdict(set)
    for t in tables:
        for l in t.findall("d:LCN", doc.nsmap):
            idx[l.get("serviceRef")].add(int(l.get("channelNumber")))
    if ref is not None:  # Services, die nur regional vorkommen
        for t in doc.lcn_tables:
            for l in t.findall("d:LCN", doc.nsmap):
                if l.get("serviceRef") not in idx:
                    idx.setdefault(l.get("serviceRef") + "#regional", set()).add(int(l.get("channelNumber")))
    return idx


def build_view(rr: RunResult) -> dict:
    doc = rr.doc
    mods = [m for m in rr.modules]
    lcn = _lcn_index(rr)
    worst: dict[tuple[str, str], Severity] = {}
    nerr = defaultdict(int)
    for f in rr.findings:
        if f.service:
            k = (f.service, f.module)
            if k not in worst or f.severity > worst[k]:
                worst[k] = f.severity
            if f.severity == Severity.ERROR:
                nerr[f.service] += 1
    rows = []
    for s in doc.services:
        lc = sorted(lcn.get(s.uid, set()))
        regional = False
        if not lc:
            lc = sorted(lcn.get(s.uid + "#regional", set()))
            regional = bool(lc)
        cells = []
        for m in mods:
            if m.skipped:
                cells.append("skip")
            else:
                sev = worst.get((s.uid, m.name))
                cells.append({None: "ok", Severity.INFO: "ok", Severity.WARNING: "warn",
                              Severity.ERROR: "err"}[sev])
        rows.append({"uid": s.uid, "name": s.name, "provider": s.provider,
                     "type": {"linear": "TV", "linear-tv": "TV", "linear-radio": "Radio"}.get(s.service_type or "", s.service_type or "–"),
                     "lcn": lc[0] if lc else None, "lcn_all": lc, "regional": regional,
                     "cells": cells, "errors": nerr.get(s.uid, 0)})
    rows.sort(key=lambda r: (r["lcn"] is None, r["lcn"] or 0, r["name"].lower()))
    svc_total = len(rows)
    svc_clean = sum(1 for r in rows if r["errors"] == 0)

    groups_by_module = OrderedDict()
    for m in mods:
        g = OrderedDict()
        for f in sorted(m.findings, key=lambda x: (SEV_ORDER[x.severity], x.code)):
            key = (f.code, f.severity, f.message)
            if key not in g:
                g[key] = {"code": f.code, "severity": f.severity.name.lower(), "sev_label": f.severity.label,
                          "message": f.message, "ref": f.ref, "items": []}
            g[key]["items"].append({"subject": f.subject, "detail": f.detail, "service": f.service or ""})
        groups_by_module[m.name] = list(g.values())

    cg = rr.module("cg")
    cg_rows, cg_days = [], []
    if cg and not cg.skipped:
        past, fut = rr.cfg.cg_days_past, rr.cfg.cg_days_future
        cg_days = list(range(-past, fut + 1))
        defaults = {"sid": None, "now_next": None, "now_title": None, "avg_coverage": None,
                    "program_info": None, "avg_response_s": None, "days": {}}
        for s in cg.stats.get("per_service", []):
            row = {**defaults, **s}
            days = row["days"] or {}
            cg_rows.append({**row, "day_cells": [days.get(d) for d in cg_days]})
    img = rr.module("images")
    img_rows = img.stats.get("images", []) if img and not img.skipped else []

    tz = ZoneInfo("Europe/Berlin")
    return {
        "rr": rr, "meta": rr.meta, "list": rr.meta.get("list", {}), "mods": mods, "rows": rows,
        "groups": groups_by_module, "titles": MODULE_TITLES,
        "counts": {s.name.lower(): rr.count(s) for s in Severity},
        "svc_total": svc_total, "svc_clean": svc_clean,
        "readiness": round(100 * svc_clean / svc_total) if svc_total else 0,
        "cg_rows": cg_rows, "cg_days": cg_days, "img_rows": img_rows, "cfg": rr.cfg,
        "started": rr.started.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
        "duration": round((rr.finished - rr.started).total_seconds(), 1) if rr.finished else None,
        "schema": rr.module("schema").stats if rr.module("schema") else {},
        "profile_stats": rr.module("profile").stats if rr.module("profile") else {},
    }


def render_html(rr: RunResult) -> str:
    env = Environment(loader=PackageLoader("dvbi_check", "report/templates"),
                      autoescape=select_autoescape(["html", "j2"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["pct"] = lambda v: f"{v * 100:.0f} %" if isinstance(v, (int, float)) else "–"
    return env.get_template("report.html.j2").render(**build_view(rr))
