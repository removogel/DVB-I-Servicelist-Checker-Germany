"""Semantic checks according to DVB A177r6 (ETSI TS 103 770 V1.2.1).

Rules that the German profile explicitly relaxes (DVB-T/C wildcard triplets,
NetworkID) are downgraded to info when profile="de".
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..model import Findings, ModuleResult, Severity, Timer
from ..resources import load_classification_schemes
from ..sl import XML_LANG, howrelated, local
from . import Context

TAG_URI = re.compile(r"^tag:[A-Za-z0-9.\-@_]+,\d{4}(-\d{2}(-\d{2})?)?:.+$", re.I)
CS_HREF = re.compile(r"^(urn:.+?:\d{4}(?:-\d{2})?):(.+)$")
DELIVERY = ["DASHDeliveryParameters", "DVBTDeliveryParameters", "DVBSDeliveryParameters",
            "DVBCDeliveryParameters", "SATIPDeliveryParameters", "RTSPDeliveryParameters",
            "MulticastTSDeliveryParameters", "IdentifierBasedDeliveryParameters", "OtherDeliveryParameters"]
PLZ = re.compile(r"^\d{5}$")


def run(ctx: Context) -> ModuleResult:
    res = ModuleResult("a177")
    f = Findings("a177")
    with Timer() as t:
        doc, ns = ctx.doc, ctx.doc.nsmap
        de = ctx.cfg.profile == "de"
        _classification_schemes(ctx, f)
        _header(ctx, f)
        _regions(ctx, f)
        _cg_sources(ctx, f)
        _services(ctx, f)
        _instances(ctx, f, de)
        _related_material(ctx, f)
        _lcn(ctx, f)
        res.stats = {
            "services": len(doc.services),
            "service_types": dict(Counter(s.service_type or "(none)" for s in doc.services)),
            "instances": len(doc.xr("//d:ServiceInstance")),
            "delivery": dict(Counter(local(p) for p in doc.xr("//d:ServiceInstance/*")
                                     if local(p).endswith("DeliveryParameters"))),
            "lcn_tables": len(doc.lcn_tables),
            "lcn_entries": len(doc.xr("//d:LCN")),
            "regions": len(doc.regions),
            "cg_sources": len(doc.cg_sources),
            "providers": dict(Counter(s.provider for s in doc.services)),
        }
    res.findings = f
    res.duration = t.elapsed
    return res


# --------------------------------------------------------------------------
def _classification_schemes(ctx: Context, f: Findings) -> None:
    if ctx.res is None:
        f.info("CS00", "Classification schemes not checked (no resources)")
        return
    terms = load_classification_schemes(str(ctx.res.cs))
    unknown = Counter()
    for el in ctx.doc.root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("href", "purpose"):
            v = el.get(attr)
            if not v or not v.startswith("urn:") or ":cs:" not in v:
                continue
            m = CS_HREF.match(v)
            if not m:
                continue
            scheme, term = m.groups()
            if scheme not in terms:
                unknown[scheme] += 1
            elif term not in terms[scheme]:
                svc = ctx.doc.service_of(el)
                f.error("CS01", "Value does not exist in the classification scheme", ref="A177 (Classification Schemes)",
                        service=svc.uid if svc else None, subject=svc.name if svc else local(el), detail=v)
    for scheme, n in unknown.items():
        f.info("CS02", "Classification scheme not in the reference data (not verified)",
               subject=scheme, detail=f"used {n}×")


def _header(ctx: Context, f: Findings) -> None:
    doc = ctx.doc
    for tag in ("Name", "ProviderName"):
        if doc.root.find(f"d:{tag}", doc.nsmap) is None:
            f.error("HD01", f"ServiceList without {tag}", ref="A177")
    if not doc.root.get("version"):
        f.error("HD02", "ServiceList without @version", ref="A177")
    if not doc.root.get(XML_LANG):
        f.warning("HD03", "ServiceList without @xml:lang", ref="A177 / DTVP IT-6")
    if not any(re.search(r"1001\.1$", h.get("href", "")) for h in doc.xr("d:RelatedMaterial/t:HowRelated")):
        f.info("HD04", "No logo for the service list itself (RelatedMaterial, HowRelated 1001.1)")


def _regions(ctx: Context, f: Findings) -> None:
    doc = ctx.doc
    regions = doc.xr("d:RegionList//d:Region")
    for rid, n in Counter(r.get("regionID") for r in regions).items():
        if n > 1:
            f.error("RG01", "regionID assigned more than once", subject=rid)
    for r in regions:
        rid = r.get("regionID")
        if doc.region_depth(r) > 4:
            f.error("RG02", "Region nested deeper than 3 sub-levels", ref="A177 Tab. 33", subject=rid)
        if r.find("d:RegionName", doc.nsmap) is None:
            f.warning("RG03", "Region without RegionName", subject=rid)
        if r.get("selectable") == "false" and r.find("d:Region", doc.nsmap) is None:
            f.error("RG08", "Leaf region with selectable=\"false\" cannot be selected", subject=rid)
        for pc in r.findall("d:Postcode", doc.nsmap):
            v = (pc.text or "").strip()
            if "*" in v:
                if not re.match(r"^\d{0,4}\*\d{0,4}$", v):
                    f.warning("RG05", "Unusual postcode wildcard", subject=rid, detail=v)
            elif not PLZ.match(v):
                f.warning("RG04", "Postcode is not a 5-digit German postcode", subject=rid, detail=f"'{v}'")
        for pr in r.findall("d:PostcodeRange", doc.nsmap):
            a, b = pr.get("from", ""), pr.get("to", "")
            if not (PLZ.match(a) and PLZ.match(b)):
                f.error("RG06", "PostcodeRange without valid 5-digit postcodes", subject=rid, detail=f"{a}–{b}")
            elif int(a) > int(b):
                f.error("RG06", "PostcodeRange with from > to", subject=rid, detail=f"{a}–{b}")

    def plz_set(r):
        s = set()
        for pc in r.findall("d:Postcode", doc.nsmap):
            v = (pc.text or "").strip()
            if PLZ.match(v):
                s.add(int(v))
        for pr in r.findall("d:PostcodeRange", doc.nsmap):
            a, b = pr.get("from", ""), pr.get("to", "")
            if PLZ.match(a) and PLZ.match(b) and int(a) <= int(b):
                s.update(range(int(a), int(b) + 1))
        return s

    siblings = defaultdict(list)
    for r in regions:
        siblings[id(r.getparent())].append(r)
    for group in siblings.values():
        sets = [(r.get("regionID"), plz_set(r)) for r in group]
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                inter = sets[i][1] & sets[j][1]
                if inter:
                    ex = ", ".join(f"{x:05d}" for x in sorted(inter)[:5])
                    f.warning("RG07", "Sibling regions overlap in postcodes (ambiguous assignment)",
                              subject=f"{sets[i][0]} ∩ {sets[j][0]}", detail=f"{len(inter)} postcodes, e.g. {ex}")


def _cg_sources(ctx: Context, f: Findings) -> None:
    doc = ctx.doc
    ids = Counter(c.get("CGSID") for c in doc.xr("//d:ContentGuideSource"))
    for k, n in ids.items():
        if n > 1 and k:
            f.error("CG01", "CGSID assigned more than once", subject=k)
    for c in doc.xr("//d:ContentGuideSource"):
        cid = c.get("CGSID") or "(inline)"
        if c.find("d:ScheduleInfoEndpoint", doc.nsmap) is None:
            f.error("CG02", "ContentGuideSource without ScheduleInfoEndpoint", subject=cid)
        for u in c.iterfind(".//ty:URI", doc.nsmap):
            if (u.text or "").startswith("http://"):
                f.warning("CG03", "Content guide endpoint without HTTPS", subject=cid, detail=u.text)


def _services(ctx: Context, f: Findings) -> None:
    doc, ns = ctx.doc, ctx.doc.nsmap
    for uid, n in Counter(s.uid for s in doc.services).items():
        if n > 1:
            f.error("SV01", "UniqueIdentifier assigned more than once", service=uid, subject=uid)
    has_list_cgs = doc.root.find("d:ContentGuideSource", ns) is not None
    for s in doc.services:
        kw = {"service": s.uid, "subject": s.name}
        if s.uid.lower().startswith("tag:") and not TAG_URI.match(s.uid):
            f.error("SV02", "UniqueIdentifier is not a valid tag: URI (RFC 4151, date missing)",
                    ref="RFC 4151", detail=s.uid, **kw)
        if s.service_type is None:
            f.warning("SV03", "Service without ServiceType (TV/radio not distinguishable)", **kw)
        if not s.el.get("version"):
            f.info("SV04", "Service without @version", **kw)
        logos = doc.logo_uris(s)
        if not logos:
            f.warning("SV05", "Service without logo (HowRelated 1001.2)", **kw)
        for uri, ct in logos:
            if uri.startswith("http://"):
                f.warning("SV06", "Logo URL without HTTPS", detail=uri, **kw)
            if not ct.startswith("image/"):
                f.warning("SV07", "Logo without or with wrong contentType", detail=f"'{ct}' {uri}", **kw)
            else:
                m = re.search(r"\.(png|jpe?g|svg|webp|gif)(\?|$)", uri.lower())
                if m:
                    exp = "image/" + m.group(1).replace("jpg", "jpeg").replace("svg", "svg+xml")
                    if ct != exp:
                        f.warning("SV07", "Declared contentType does not match the file extension",
                                  detail=f"{ct} ↔ {uri.rsplit('/', 1)[-1]}", **kw)
        ref = doc.text(s.el, "d:ContentGuideSourceRef")
        if ref and ref not in doc.cg_sources:
            f.error("SV08", "ContentGuideSourceRef refers to an unknown ContentGuideSource", detail=ref, **kw)
        if not ref and s.el.find("d:ContentGuideSource", ns) is None and not has_list_cgs:
            f.warning("SV09", "Service without content guide source (no EPG)", **kw)


def _instances(ctx: Context, f: Findings, de: bool) -> None:
    doc, ns = ctx.doc, ctx.doc.nsmap
    relaxed = Severity.INFO if de else Severity.WARNING
    for s in doc.services:
        kw = {"service": s.uid, "subject": s.name}
        insts = s.el.findall("d:ServiceInstance", ns)
        if not insts:
            f.error("SI01", "Service without ServiceInstance (not playable)", **kw)
            continue
        prios = Counter(i.get("priority") for i in insts if i.get("priority"))
        for p, n in prios.items():
            if n > 1:
                f.warning("SI04", "Several ServiceInstances with the same @priority (order undefined)",
                          ref="A177 5.2.13", detail=f"priority {p} {n}×", **kw)
        any_delivery = False
        for i in insts:
            dps = [local(c) for c in i if local(c) in DELIVERY]
            any_delivery |= bool(dps)
            if not dps and i.find("d:ContentProtection", ns) is not None:
                f.info("SI03", "ContentProtection in a ServiceInstance without delivery parameters (no effect)",
                       detail=f"priority {i.get('priority')}", **kw)
            c = i.find("d:DVBCDeliveryParameters", ns)
            if c is not None and c.find("d:NetworkID", ns) is None:
                f.add("SI05", relaxed, "DVBCDeliveryParameters without NetworkID"
                      + (" – permitted in the DE profile (IT-12)" if de else " – mandatory per standard schema"),
                      ref="A177 5.5.18.3 / DTVP IT-12", **kw)
            for kind in ("DVBT", "DVBC", "DVBS"):
                p = i.find(f"d:{kind}DeliveryParameters", ns)
                if p is None:
                    continue
                tr = p.find("d:DVBTriplet", ns)
                if tr is None:
                    continue
                miss = [a for a in ("origNetId", "tsId") if tr.get(a) is None]
                if miss:
                    sev = relaxed if kind in ("DVBT", "DVBC") else Severity.WARNING
                    f.add(f"SI06-{kind}", sev, f"{kind} triplet without {'/'.join(miss)} (wildcard)"
                          + (" – intended in the DE profile" if de and kind != "DVBS" else ""),
                          ref="A177 5.2.1 / DTVP IT-11, IT-12", **kw)
            dash = i.find("d:DASHDeliveryParameters", ns)
            if dash is not None:
                for ul in dash.findall("d:UriBasedLocation", ns):
                    uri = doc.text(ul, "ty:URI") or ""
                    if uri.startswith("http://"):
                        f.warning("SI07", "DASH MPD without HTTPS", detail=uri, **kw)
                    if ul.get("contentType") != "application/dash+xml":
                        f.error("SI08", "DASH UriBasedLocation with wrong contentType",
                                detail=str(ul.get("contentType")), **kw)
        if not any_delivery:
            apps = [a for i in insts for a in doc.linked_apps(i)] + doc.linked_apps(s.el)
            if "1.2" in apps:
                f.info("SI02", "Only a linked application instance (type 1.2), no native delivery parameters", **kw)
            else:
                f.warning("SI02", "No instance with delivery parameters and no linked application 1.2", **kw)


# --------------------------------------------------------------------------
# RelatedMaterial placement – mirrors the DVB-I reference validator (dvb-i-tools),
# which cites A177 tables 14 (ServiceList), 15 (Service), 16 (ServiceInstance) and
# 20 (ContentGuideSource).
APP_CONTENT_TYPES = {"application/vnd.dvb.ait+xml", "text/html", "application/xhtml+xml", "application/xml"}
LOCATION_REF = {"ServiceList": "A177 table 14", "Service": "A177 table 15",
                "ServiceInstance": "A177 table 16", "ContentGuideSource": "A177 table 20"}


def _permitted(doc, location: str) -> tuple[set[str], set[str]]:
    """(HowRelatedCS terms, LinkedApplicationCS terms) permitted at a location for this revision."""
    apps = {"1.1", "1.2"}
    if doc.rev_at_least("2024"):      # A177r6: service provider app
        apps.add("3")
    if doc.rev_at_least("2025"):      # A177r7: app in series
        apps.add("1.3")
    if location == "ServiceList":
        return {"1001.1"}, ({"4.1", "4.2", "4.3"} if doc.rev_at_least("2025") else set())
    if location == "Service":
        hr = {"1000.1", "1001.2"}
        if doc.rev_at_least("2020"):
            hr.add("1000.2")
        if doc.rev_at_least("2021"):
            hr.add("1001.3")
        return hr, apps | {"2"}
    if location == "ServiceInstance":
        hr = {"1001.2"}
        if doc.rev_at_least("2020"):
            hr.add("1000.2")
        if not doc.rev_at_least("2024"):  # out-of-service banner no longer permitted in instances from r6
            hr.add("1000.1")
        return hr, apps
    if location == "ContentGuideSource":
        return {"1002.1"}, set()
    return set(), set()


def _expected_cs_year(doc) -> str:
    return "2021" if doc.rev_at_least("2022") else "2020" if doc.rev_at_least("2020") else "2019"


def _related_material(ctx: Context, f: Findings) -> None:
    doc, ns = ctx.doc, ctx.doc.nsmap
    codes = {"ServiceList": "RM01", "Service": "RM02", "ServiceInstance": "RM03", "ContentGuideSource": "RM04"}
    for rm in doc.root.iter(f"{{{doc.ns}}}RelatedMaterial"):
        location = local(rm.getparent())
        if location not in codes:
            continue
        svc = doc.service_of(rm)
        kw = {"service": svc.uid if svc else None,
              "subject": svc.name if svc else (rm.getparent().get("CGSID") or location)}
        where = location + (f" priority {rm.getparent().get('priority')}" if location == "ServiceInstance" else "")
        code, ref = codes[location], LOCATION_REF[location]
        hr_el = rm.find("t:HowRelated", ns)
        href = hr_el.get("href", "") if hr_el is not None else ""
        if not href:
            f.error(code, f"RelatedMaterial in {location} without HowRelated@href", ref=ref, detail=where, **kw)
            continue
        scheme, term = howrelated(href)
        allowed_hr, allowed_apps = _permitted(doc, location)
        if scheme == "HowRelatedCS":
            year = href.split(":")[-2]
            if term in allowed_hr and year != _expected_cs_year(doc):
                f.error("RM06", f"HowRelatedCS:{year} is not valid for {doc.revision} "
                                f"(expected HowRelatedCS:{_expected_cs_year(doc)})", ref=ref, detail=f"{where}: {href}", **kw)
            ok = term in allowed_hr
        elif scheme == "LinkedApplicationCS":
            ok = term in allowed_apps
        else:
            ok = False
        if not ok:
            f.error(code, f"HowRelated value not permitted in {location}", ref=ref, detail=f"{where}: {href}", **kw)
            continue
        if scheme == "LinkedApplicationCS":
            _check_app_locator(rm, href, term, where, kw, f, ns)


def _check_app_locator(rm, href, term, where, kw, f, ns) -> None:
    uris = rm.findall("t:MediaLocator/t:MediaUri", ns)
    if not uris:
        f.error("RM05", "Signalled application without MediaUri", ref="A177", detail=f"{where}: {href}", **kw)
    for mu in uris:
        url, ct = (mu.text or "").strip(), mu.get("contentType", "")
        if ct and ct not in APP_CONTENT_TYPES:
            f.error("RM05", "Application MediaUri@contentType is not a permitted application type", ref="A177",
                    detail=f"{where}: contentType '{ct}' · {url}", **kw)
        if term == "3" and ct and ct != "application/vnd.dvb.ait+xml":
            f.error("RM05", "Service provider application (type 3) must use an XML AIT", ref="A177",
                    detail=f"{where}: contentType '{ct}' · {url}", **kw)
        if not re.match(r"^https?://\S+$", url):
            f.error("RM05", "Application MediaUri is not a valid HTTP(S) URL", ref="A177", detail=f"{where}: {url!r}", **kw)
        elif not url.isascii():
            f.error("RM05", "Application MediaUri contains non-ASCII characters", ref="A177", detail=f"{where}: {url}", **kw)


def _lcn(ctx: Context, f: Findings) -> None:
    doc, ns = ctx.doc, ctx.doc.nsmap
    in_any = set()
    combos = {}
    for idx, t in enumerate(doc.lcn_tables, 1):
        label = doc.table_label(idx, t)
        trg = [x.text for x in t.findall("d:TargetRegion", ns)]
        for r in trg:
            if r not in doc.regions:
                f.error("LC04", "TargetRegion refers to an unknown regionID", subject=label, detail=r)
        pk = [x.text for x in t.findall("d:SubscriptionPackage", ns)] or ["–"]
        for r in (trg or ["–"]):
            for p in pk:
                if (r, p) in combos:
                    f.error("LC07", "TargetRegion/SubscriptionPackage combination used in several LCNTables",
                            subject=label, detail=f"also in LCNTable #{combos[(r, p)]}")
                else:
                    combos[(r, p)] = idx
        chans, refs = Counter(), Counter()
        for l in t.findall("d:LCN", ns):
            ref, ch = l.get("serviceRef"), l.get("channelNumber")
            chans[ch] += 1
            refs[ref] += 1
            in_any.add(ref)
            if ref not in doc.by_uid:
                f.error("LC01", "LCN refers to an unknown service", subject=label, detail=f"LCN {ch} → {ref}")
            n = int(ch)
            if n < 1 or n > 9999:
                f.error("LC08", "Channel number outside 1–9999", subject=label, detail=ch)
        for ch, n in chans.items():
            if n > 1:
                f.error("LC02", "Channel number assigned more than once", subject=label, detail=f"LCN {ch} {n}×")
        for ref, n in refs.items():
            if n > 1:
                svc = doc.by_uid.get(ref)
                f.warning("LC03", "Service listed more than once in the same LCNTable", service=ref,
                          subject=svc.name if svc else ref, detail=f"{label}, {n}×")
    untargeted = [i for i, t in enumerate(doc.lcn_tables, 1) if t.find("d:TargetRegion", ns) is None]
    if len(untargeted) > 1:
        f.warning("LC05", "Several LCNTables without TargetRegion", detail=", ".join(f"#{i}" for i in untargeted))
    for s in doc.services:
        if s.uid not in in_any:
            f.warning("LC06", "Service not in any LCNTable (no channel number)", service=s.uid, subject=s.name)
