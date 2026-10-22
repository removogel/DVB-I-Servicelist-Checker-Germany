"""Schema validation of the service list against the A177 XSD (standard and German profile)."""
from __future__ import annotations

import re

from lxml import etree

from ..model import Findings, ModuleResult, Timer
from ..resources import load_schema
from . import Context

NS_RE = re.compile(r"\{[^}]*\}")


def _short(msg: str) -> str:
    return NS_RE.sub("", msg).replace("Schemas validity error : ", "")


def validate(ctx: Context, xsd_path, label: str, f: Findings, code: str, as_error: bool) -> tuple[bool, int]:
    schema = load_schema(str(xsd_path))
    ok = schema.validate(ctx.doc.tree)
    if ok:
        return True, 0
    for e in schema.error_log:
        el = None
        try:
            found = ctx.doc.tree.xpath(e.path) if e.path else []
            el = found[0] if found else None
        except etree.XPathError:
            pass
        svc = ctx.doc.service_of(el) if el is not None else None
        add = f.error if as_error else f.warning
        add(code, _short(e.message), ref=label, service=svc.uid if svc else None,
            subject=svc.name if svc else (e.path or f"line {e.line}"),
            detail=f"path {e.path}, line {e.line}")
    return False, len(schema.error_log)


def run(ctx: Context) -> ModuleResult:
    res = ModuleResult("schema")
    f = Findings("schema")
    with Timer() as t:
        doc = ctx.doc
        res.stats.update({"namespace": doc.ns, "revision": doc.revision, "bytes": len(doc.raw)})
        f.info("SC00", f"Well-formed; namespace {doc.ns} ({doc.revision})", ref="A177")

        if ctx.res is None:
            res.skipped = "No schemas available (see 'dvbi-check resources update')"
        else:
            std, de = ctx.res.sl_schema_files(doc.ns_year)
            if std is None:
                f.error("SC01", f"No schema registered for namespace {doc.ns}", ref="A177")
            else:
                use_de = ctx.cfg.profile == "de" and de is not None
                # With the DE profile the German schema is authoritative; deviations from the standard are reported separately.
                ok_std, n_std = validate(ctx, std, f"XSD {std.name}", f, "SC10", as_error=not use_de)
                res.stats["valid_standard"] = ok_std
                res.stats["errors_standard"] = n_std
                if de is not None:
                    ok_de, n_de = validate(ctx, de, f"XSD {de.name}", f, "SC20", as_error=use_de)
                    res.stats["valid_de_profile"] = ok_de
                    res.stats["errors_de_profile"] = n_de
                if use_de:
                    verdict = ("valid (standard and DE profile)" if ok_std and res.stats["valid_de_profile"]
                               else "valid against DE profile schema only" if res.stats["valid_de_profile"]
                               else "not valid")
                    if not ok_std and res.stats["valid_de_profile"]:
                        f.info("SC30", f"{n_std} deviation(s) from the standard schema that the German "
                                       "profile schema permits. Clients validating strictly against the "
                                       "standard might reject the list.", ref="DTVP IT-12 / XSD (DE)")
                else:
                    verdict = "valid" if ok_std else "not valid"
                res.stats["verdict"] = verdict

        # Delivery (only when fetched via URL)
        if ctx.source.is_url and ctx.source.http is not None:
            ct = ctx.source.http.content_type
            res.stats["http"] = ctx.source.meta
            if ct not in ("application/vnd.dvb.dvbisl+xml", "application/xml", "text/xml"):
                f.warning("SC40", f"Unexpected Content-Type '{ct}' (expected application/vnd.dvb.dvbisl+xml)",
                          ref="A177 (service list MIME type)")
            elif ct != "application/vnd.dvb.dvbisl+xml":
                f.info("SC41", f"Content-Type '{ct}' instead of application/vnd.dvb.dvbisl+xml", ref="A177 (service list MIME type)")
            if ctx.source.request_url and ctx.source.request_url.startswith("http://"):
                f.warning("SC42", "Service list is delivered without HTTPS", ref="A177")
    res.findings = f
    res.duration = t.elapsed
    return res
