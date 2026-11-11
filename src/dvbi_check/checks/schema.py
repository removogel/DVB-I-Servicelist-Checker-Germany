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


def validate(ctx: Context, xsd_path, label: str, f: Findings | None, code: str, as_error: bool,
             only_paths: set | None = None) -> tuple[bool, int, set]:
    """Validates the list; reports each error (all, or only those at `only_paths`) and returns
    (valid, number of errors, error paths)."""
    schema = load_schema(str(xsd_path))
    ok = schema.validate(ctx.doc.tree)
    if ok:
        return True, 0, set()
    paths = {e.path for e in schema.error_log}
    for e in schema.error_log:
        if f is None or (only_paths is not None and e.path not in only_paths):
            continue
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
    return False, len(schema.error_log), paths


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
                de_paths: set = set()
                if de is not None:
                    ok_de, n_de, de_paths = validate(ctx, de, f"XSD {de.name}", f, "SC20", as_error=use_de)
                    res.stats["valid_de_profile"] = ok_de
                    res.stats["errors_de_profile"] = n_de
                # With the DE profile the German schema is authoritative: deviations from the standard schema
                # that the DE profile schema permits (e.g. DVB-C without NetworkID, DTVP IT-12) are not
                # reported individually, only counted in SC30. Standard errors at places where the DE schema
                # fails as well remain warnings next to the SC20 error.
                ok_std, n_std, std_paths = validate(ctx, std, f"XSD {std.name}", f, "SC10", as_error=not use_de,
                                                    only_paths=de_paths if use_de else None)
                res.stats["valid_standard"] = ok_std
                res.stats["errors_standard"] = n_std
                if use_de:
                    verdict = ("valid (standard and DE profile)" if ok_std and res.stats["valid_de_profile"]
                               else "valid against DE profile schema only" if res.stats["valid_de_profile"]
                               else "not valid")
                    permitted = sum(1 for e in std_paths if e not in de_paths)
                    res.stats["deviations_permitted_by_de"] = permitted
                    if permitted:
                        f.info("SC30", f"{permitted} place(s) deviate from the standard schema in a way the German "
                                       "profile schema permits (not listed individually). Clients validating "
                                       "strictly against the standard might reject them.",
                               ref="DTVP IT-12 / XSD (DE)")
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
