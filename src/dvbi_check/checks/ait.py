"""Checks of the XML AITs of linked applications (HbbTV).

Basis: ETSI TS 102 809 (XML AIT, namespace urn:dvb:mhp:2009),
ETSI TS 102 796 / HbbTV 2.0.4 (application type application/vnd.hbbtv.xhtml+xml),
A177r6 linked applications (LinkedApplicationCS:2019) and DTVP IT-3/IT-6.

Without an official XSD, structure and mandatory fields are checked directly. If
`ait_xsd` is configured (e.g. mis_xmlait.xsd from TS 102 809), the AIT is also
validated against the schema.
"""
from __future__ import annotations

import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urljoin

from lxml import etree

from ..model import Findings, ModuleResult, Severity, Timer
from ..resources import load_schema
from ..sl import LINKED_APP, OOS_APP, Service
from . import Context

MHP_NS = "urn:dvb:mhp:2009"
AIT_CT = "application/vnd.dvb.ait+xml"
HBBTV_APP = "application/vnd.hbbtv.xhtml+xml"
CONTROL_CODES = {"AUTOSTART", "PRESENT", "DESTROY", "KILL", "PREFETCH", "REMOTE", "DISABLED", "PLAYBACK_AUTOSTART"}
REF_AIT = "ETSI TS 102 809 (XML AIT)"
REF_HBB = "HbbTV / ETSI TS 102 796"
APP_KINDS = {"1.1": "Linked App 1.1 (parallel)", "1.2": "Linked App 1.2 (media playback)",
             "1.3": "Linked App 1.3", "2": "Out-of-service app", "3": "Provider app"}


@dataclass
class AitReport:
    url: str
    declared_ct: str
    kinds: set = field(default_factory=set)
    issues: list = field(default_factory=list)
    status: int | None = None
    content_type: str = ""
    elapsed: float = 0.0
    apps: list = field(default_factory=list)

    def add(self, code, sev, msg, ref="", detail=""):
        self.issues.append((code, sev, msg, ref, detail))

    def to_dict(self):
        return {"url": self.url, "status": self.status, "content_type": self.content_type,
                "elapsed_s": round(self.elapsed, 2), "kinds": sorted(self.kinds), "apps": self.apps,
                "issues": len(self.issues)}


def _l(el) -> str:
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _k(el, name):
    return el.find(f"{{{MHP_NS}}}{name}") if el is not None else None


def _ks(el, name):
    return el.findall(f"{{{MHP_NS}}}{name}") if el is not None else []


def _t(el, name) -> str | None:
    k = _k(el, name)
    return k.text.strip() if k is not None and k.text and k.text.strip() else None


def check_ait(ctx: Context, url: str, declared_ct: str, kinds: set) -> AitReport:
    rep = AitReport(url, declared_ct, set(kinds))
    cfg = ctx.cfg
    if url.startswith("http://"):
        rep.add("AIT-HTTPS", Severity.WARNING, "AIT URL without HTTPS")
    if declared_ct and declared_ct != AIT_CT:
        rep.add("AIT-DECL-CT", Severity.WARNING, f"contentType declared in the service list is not {AIT_CT}",
                ref="A177 (Linked Application) / " + REF_HBB, detail=declared_ct)
    r = ctx.client.get(url)
    rep.status, rep.elapsed, rep.content_type = r.status, r.elapsed, r.content_type
    if not r.ok:
        rep.add("AIT-HTTP", Severity.ERROR, "AIT not retrievable", detail=r.describe())
        return rep
    if rep.content_type != AIT_CT:
        sev = Severity.WARNING if "xml" in rep.content_type else Severity.ERROR
        rep.add("AIT-CT", sev, f"Server returns Content-Type '{rep.content_type or '(empty)'}' instead of {AIT_CT}",
                ref=REF_HBB)
    try:
        root = etree.fromstring(r.content, etree.XMLParser(no_network=True, resolve_entities=False))
    except etree.XMLSyntaxError as e:
        rep.add("AIT-XML", Severity.ERROR, "AIT is not well-formed XML", detail=str(e))
        return rep
    q = etree.QName(root)
    if q.localname != "ServiceDiscovery" or q.namespace != MHP_NS:
        rep.add("AIT-ROOT", Severity.ERROR, f"Root element is not ServiceDiscovery in namespace {MHP_NS}",
                ref=REF_AIT, detail=root.tag)
        return rep
    if cfg.ait_xsd:
        try:
            schema = load_schema(str(cfg.ait_xsd))
            if not schema.validate(root.getroottree()):
                for e in list(schema.error_log)[:5]:
                    rep.add("AIT-XSD", Severity.ERROR, "Schema error in XML AIT", ref=REF_AIT,
                            detail=re.sub(r"\{[^}]*\}", "", e.message))
        except Exception as e:  # noqa: BLE001
            rep.add("AIT-XSD", Severity.INFO, "AIT schema could not be loaded", detail=str(e))

    apps = root.findall(f"{{{MHP_NS}}}ApplicationDiscovery/{{{MHP_NS}}}ApplicationList/{{{MHP_NS}}}Application")
    ad = _k(root, "ApplicationDiscovery")
    if ad is not None and not ad.get("DomainName"):
        rep.add("AIT-DOMAIN", Severity.WARNING, "ApplicationDiscovery without @DomainName", ref=REF_AIT)
    if not apps:
        rep.add("AIT-NOAPP", Severity.ERROR, "AIT contains no Application", ref=REF_AIT)
        return rep
    autostart = 0
    for i, app in enumerate(apps, 1):
        info = {"name": _t(app, "appName")}
        tag = f"Application {i}" + (f" '{info['name']}'" if info["name"] else "")
        aid = _k(app, "applicationIdentifier")
        org, appid = _t(aid, "orgId"), _t(aid, "appId")
        info["orgId"], info["appId"] = org, appid
        if not (org and org.isdigit() and appid and appid.isdigit()):
            rep.add("AIT-ID", Severity.ERROR, "applicationIdentifier without valid orgId/appId", ref=REF_AIT,
                    detail=f"{tag}: orgId={org}, appId={appid}")
        desc = _k(app, "applicationDescriptor")
        if desc is None:
            rep.add("AIT-DESC", Severity.ERROR, "applicationDescriptor missing", ref=REF_AIT, detail=tag)
            continue
        app_type = _t(_k(desc, "type"), "OtherApp")
        info["type"] = app_type
        if app_type != HBBTV_APP:
            rep.add("AIT-TYPE", Severity.ERROR, f"Application type is not {HBBTV_APP}", ref="DTVP IT-3 / " + REF_HBB,
                    detail=f"{tag}: {app_type}")
        cc = _t(desc, "controlCode")
        info["controlCode"] = cc
        if cc not in CONTROL_CODES:
            rep.add("AIT-CC", Severity.ERROR, "controlCode missing or invalid", ref=REF_AIT, detail=f"{tag}: {cc}")
        if cc == "AUTOSTART":
            autostart += 1
        for el in ("visibility", "priority", "version"):
            if _t(desc, el) is None:
                rep.add("AIT-DESC-FIELD", Severity.WARNING, f"applicationDescriptor without {el}", ref=REF_AIT, detail=tag)
        mv = _k(desc, "mhpVersion")
        if mv is None:
            rep.add("AIT-MHPVER", Severity.WARNING, "applicationDescriptor without mhpVersion (HbbTV profile/version)",
                    ref=REF_HBB, detail=tag)
        else:
            info["version"] = ".".join(x for x in (_t(mv, "versionMajor"), _t(mv, "versionMinor"),
                                                   _t(mv, "versionMicro")) if x)
        # transport and entry point
        transports = _ks(app, "applicationTransport")
        base = None
        for t in transports:
            xt = t.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
            if xt.endswith("HTTPTransportType"):
                base = _t(t, "URLBase")
        loc = _t(app, "applicationLocation")
        if base is None:
            rep.add("AIT-TRANSPORT", Severity.ERROR, "No HTTPTransportType with URLBase", ref=REF_AIT, detail=tag)
        if loc is None:
            rep.add("AIT-LOC", Severity.ERROR, "applicationLocation missing", ref=REF_AIT, detail=tag)
        if base:
            entry = urljoin(base if base.endswith("/") else base + "/", loc or "")
            info["entry"] = entry
            # HTTP entry URLs are common for HbbTV apps and are deliberately not flagged.
            if cfg.ait_check_app_url and not ctx.offline:
                ra = ctx.client.get(entry)
                info["entry_status"] = ra.status
                if not ra.ok:
                    rep.add("AIT-APP-HTTP", Severity.ERROR, "Application entry URL not retrievable",
                            detail=f"{tag}: {ra.describe()} · {entry}")
                elif not any(x in ra.content_type for x in ("html", "xhtml")):
                    rep.add("AIT-APP-CT", Severity.WARNING, "Entry page does not return an HTML/XHTML Content-Type",
                            ref=REF_HBB, detail=f"{tag}: {ra.content_type} · {entry}")
        rep.apps.append(info)
    if "1.2" in rep.kinds or "1.1" in rep.kinds:
        if autostart == 0:
            rep.add("AIT-AUTOSTART", Severity.WARNING,
                    "No Application with controlCode AUTOSTART – linked application does not start automatically on channel change",
                    ref="A177 (Linked Application) / " + REF_HBB)
        elif autostart > 1:
            rep.add("AIT-AUTOSTART", Severity.WARNING, f"{autostart} Applications with AUTOSTART (ambiguous)",
                    ref=REF_HBB)
    return rep


def collect(ctx: Context) -> dict[tuple[str, str], dict]:
    """AIT URLs from all RelatedMaterial with LinkedApplicationCS – with services and app types."""
    doc = ctx.doc
    out: dict[tuple[str, str], dict] = defaultdict(lambda: {"services": [], "kinds": set()})
    for s in doc.services:
        for rm in s.el.iterfind(".//d:RelatedMaterial", doc.nsmap):
            hr = rm.find("t:HowRelated", doc.nsmap)
            href = hr.get("href", "") if hr is not None else ""
            m = LINKED_APP.search(href)
            kind = m.group(1) if m else ("2" if OOS_APP.search(href) else None)
            if kind is None and "LinkedApplicationCS" not in href:
                continue
            for mu in rm.iterfind(".//t:MediaUri", doc.nsmap):
                if not mu.text:
                    continue
                entry = out[(mu.text.strip(), mu.get("contentType", ""))]
                if s not in entry["services"]:
                    entry["services"].append(s)
                entry["kinds"].add(kind or href.rsplit(":", 1)[-1])
    return out


def run(ctx: Context) -> ModuleResult:
    res = ModuleResult("ait")
    f = Findings("ait")
    with Timer() as t:
        if ctx.offline:
            res.skipped = "Offline mode"
        else:
            targets = collect(ctx)
            ait_targets = {k: v for k, v in targets.items() if k[1] in ("", AIT_CT, "application/xml")
                           or k[0].lower().split("?")[0].endswith((".aitx", ".ait", ".xml"))}
            other = {k: v for k, v in targets.items() if k not in ait_targets}
            with ThreadPoolExecutor(max_workers=ctx.cfg.workers) as ex:
                futures = {k: ex.submit(check_ait, ctx, k[0], k[1], v["kinds"]) for k, v in ait_targets.items()}
            reports = []
            for k, fut in futures.items():
                rep = fut.result()
                reports.append(rep)
                for code, sev, msg, ref, detail in rep.issues:
                    for s in targets[k]["services"]:
                        f.add(code, sev, msg, ref=ref, service=s.uid, subject=s.name,
                              detail=(detail + " · " if detail else "") + rep.url)
            # linked apps without XML AIT (e.g. directly linked HTML): reachability only
            for (url, ct), v in other.items():
                r = ctx.client.get(url)
                for s in v["services"]:
                    f.info("AIT-NOTAIT", f"Linked application without XML AIT (contentType {ct or 'empty'})",
                           ref="A177 (Linked Application)", service=s.uid, subject=s.name, detail=url)
                    if not r.ok:
                        f.error("AIT-HTTP", "Linked application not retrievable", service=s.uid, subject=s.name,
                                detail=f"{r.describe()} · {url}")
            # for services whose only playback is a linked app 1.2, AIT errors are critical
            ok = [r for r in reports if r.status and 200 <= r.status < 300]
            res.stats = {
                "aits_checked": len(reports),
                "aits_reachable": len(ok),
                "aits_without_errors": sum(1 for r in reports if not any(i[1] == Severity.ERROR for i in r.issues)),
                "other_linked_apps": len(other),
                "avg_response_s": round(sum(r.elapsed for r in ok) / len(ok), 2) if ok else None,
                "aits": [r.to_dict() for r in reports],
            }
    res.findings = f
    res.duration = t.elapsed
    return res
