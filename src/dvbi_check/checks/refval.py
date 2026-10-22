"""Optional: run the DVB-I reference validator (paulhiggs/dvb-i-tools) via its REST interface.

The service list that dvbi-check has already loaded is POSTed as XML to
`<refval_url>/validate_sl_json?results=all`. Posting (rather than letting the validator fetch
the URL itself) also works for lists that are only delivered to whitelisted User-Agents.

Notes taken from the validator source (pinned revision, see resources.PINNED_REF):
- The REST handler does not evaluate the `variants` parameter, i.e. it validates against the
  standard A177 schema, not the German profile schema. Findings that the German profile
  explicitly permits are therefore downgraded to INFO when profile="de" (same as SI05/SI06).
- The list is re-serialised with indentation before posting so that the line numbers in the
  validator's response can be mapped back to services.

This module sends the complete service list to a third-party server. It is opt-in and only
runs when `refval_url` is configured.
"""
from __future__ import annotations

import json
import re

from lxml import etree

from ..httpclient import add_query
from ..model import Findings, ModuleResult, Severity, Timer
from ..sl import ServiceListDoc
from . import Context

REF = "DVB-I reference validator"
# Standard-schema findings that the German profile explicitly permits (DTVP IT-11/IT-12)
DE_RELAXED = re.compile(r"NetworkID|networkID|origNetId|tsId", re.I)


def _pretty(doc: ServiceListDoc) -> bytes:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=True)
    tree = etree.fromstring(doc.raw, parser).getroottree()
    return etree.tostring(tree, pretty_print=True, xml_declaration=True, encoding="UTF-8")


def _json_objects(text: str) -> list[dict]:
    """The validator may write several JSON objects back to back (parseErr + result)."""
    dec, i, out = json.JSONDecoder(), 0, []
    text = text.strip()
    while i < len(text):
        obj, j = dec.raw_decode(text, i)
        out.append(obj)
        i = j
        while i < len(text) and text[i].isspace():
            i += 1
    return out


def _line_index(pdoc: ServiceListDoc) -> dict[int, etree._Element]:
    idx: dict[int, etree._Element] = {}
    for el in pdoc.root.iter():
        if isinstance(el.tag, str) and el.sourceline not in idx:
            idx[el.sourceline] = el
    return idx


def run(ctx: Context) -> ModuleResult:
    res = ModuleResult("refval")
    f = Findings("refval")
    with Timer() as t:
        base = (ctx.cfg.refval_url or "").rstrip("/")
        if ctx.offline:
            res.skipped = "Offline mode"
        elif not base:
            res.skipped = "Not configured (--refval-url); sends the service list to an external server"
        else:
            _run(ctx, base, f, res)
    res.findings = f
    res.duration = t.elapsed
    return res


def _run(ctx: Context, base: str, f: Findings, res: ModuleResult) -> None:
    pretty = _pretty(ctx.doc)
    pdoc = ServiceListDoc(pretty, ctx.doc.source)
    lines = _line_index(pdoc)
    url = add_query(f"{base}/validate_sl_json", results="all", variants="DE" if ctx.cfg.profile == "de" else None)
    r = ctx.client.post(url, pretty, "application/xml", timeout=ctx.cfg.refval_timeout)
    res.stats = {"endpoint": url, "http_status": r.status, "elapsed_s": round(r.elapsed, 1)}
    if not r.ok:
        f.error("RV-HTTP", "Reference validator not reachable or request failed", ref=REF, detail=f"{r.describe()} · {url}")
        return
    try:
        objs = _json_objects(r.content.decode("utf-8", "replace"))
    except ValueError as e:
        f.error("RV-RESPONSE", "Reference validator returned no valid JSON", ref=REF, detail=str(e))
        return
    errs = {}
    for o in objs:
        if "parseErr" in o:
            f.error("RV-PARSE", "Reference validator could not process the list", ref=REF,
                    detail="; ".join(map(str, o["parseErr"])))
        if "errs" in o:
            errs = o["errs"] or {}
    descriptions = {d.get("code"): d for d in errs.get("errorDescriptions", []) if isinstance(d, dict)}
    de = ctx.cfg.profile == "de"
    counts = {"errors": 0, "warnings": 0, "informationals": 0, "downgraded": 0}
    for key, sev in (("fatals", Severity.ERROR), ("errors", Severity.ERROR),
                     ("warnings", Severity.WARNING), ("informationals", Severity.INFO)):
        for e in errs.get(key, []) or []:
            code, msg = str(e.get("code", "?")), str(e.get("message", "")).strip()
            line = e.get("line")
            el = lines.get(line) if isinstance(line, int) else None
            svc = pdoc.service_of(el) if el is not None else None
            fragment = str(e.get("element") or "").strip().splitlines()
            desc = descriptions.get(code, {})
            detail = f"line {line}" if line else ""
            if fragment:
                detail += (" · " if detail else "") + fragment[0][:160]
            if desc.get("description"):
                detail += (" · " if detail else "") + desc["description"][:300]
            this_sev = sev
            if de and sev != Severity.INFO and DE_RELAXED.search(msg):
                this_sev = Severity.INFO
                msg += " – permitted in the DE profile (DTVP IT-11/IT-12)"
                counts["downgraded"] += 1
            counts["errors" if key in ("fatals", "errors") else key] += 1
            f.add(f"RV-{code}", this_sev, msg, ref=desc.get("clause") or REF,
                  service=svc.uid if svc else None, subject=svc.name if svc else (f"line {line}" if line else ""),
                  detail=detail)
    res.stats.update(counts)
    if de:
        f.info("RV-VARIANT", "The reference validator's REST interface validates against the standard schema; "
                             "German profile relaxations are applied by dvbi-check", ref=REF)
