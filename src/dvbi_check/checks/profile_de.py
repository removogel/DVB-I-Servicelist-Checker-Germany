"""Vollständigkeit nach DVB-I Book Germany 1.0.1 und DTVP-Implementierungsprofil 1.0.1.

Referenzen:
  Book  = DVB-I Book Germany, Ver. 1.0.1 (01.07.2026)
  DTVP  = DTVP DVB-I Implementierungsprofil V1.0.1 (approved 03.06.2026), Implementation Topics IT-x
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from email.utils import format_datetime
from datetime import datetime, timezone

from lxml import etree

from ..httpclient import add_query
from ..model import Findings, ModuleResult, Timer
from ..sl import LINKED_APP, OOS_APP, OOS_BANNER_HOWRELATED, ServiceListDoc, Service, local
from . import Context

# Book 6.6 Listing Order Principles
BLOCKS = [
    (1, 49, "Block 1 – Public Value"), (50, 51, "LCN 50/51 – RTL/SAT.1 national"),
    (52, 199, "Block 2 – Free-TV mit AGF"), (200, 399, "Block 3 – Pay-TV mit AGF"),
    (400, 499, "Block 4 – Pay-TV ohne AGF"), (500, 999, "Block 5 – Free-TV ohne AGF"),
    (1000, 1499, "Block 6 – Regional"), (1500, 1999, "Block 7 – Pop-up"),
    (2000, 2499, "Block 8 – International"), (2500, 2999, "Block 9 – Radio"),
    (3000, 3998, "Block 10 – reserviert"), (3999, 3999, "Ende der Service List"),
]


def block_of(n: int) -> str:
    for a, b, label in BLOCKS:
        if a <= n <= b:
            return label
    return "außerhalb"


def run(ctx: Context) -> ModuleResult:
    res = ModuleResult("profile")
    f = Findings("profile")
    with Timer() as t:
        if ctx.cfg.profile != "de":
            res.skipped = "Profil 'de' nicht aktiv"
        else:
            _copyright(ctx, f)
            _mandatory_service_fields(ctx, f)
            _delivery(ctx, f)
            _content_guide_proxy(ctx, f)
            stats = _lcn_plan(ctx, f)
            _popups(ctx, f)
            _regions(ctx, f)
            if ctx.source.is_url and ctx.cfg.check_server_features and not ctx.offline:
                _server_features(ctx, f)
            res.stats = stats
    res.findings = f
    res.duration = t.elapsed
    return res


def _kw(s: Service) -> dict:
    return {"service": s.uid, "subject": s.name}


# --------------------------------------------------------------------------- Book 11.1
def _copyright(ctx: Context, f: Findings) -> None:
    head = ctx.doc.raw[:4096].decode("utf-8", "replace")
    comments = re.findall(r"<!--(.*?)-->", head, re.S) + [c.text or "" for c in ctx.doc.root.iter(etree.Comment)]
    if not any("©" in c or "copyright" in c.lower() for c in comments):
        f.error("DE-CR", "Kein Copyright-Hinweis als XML-Kommentar", ref="Book 11.1 („shall“)")


# --------------------------------------------------------------------------- DTVP IT-6
def _mandatory_service_fields(ctx: Context, f: Findings) -> None:
    doc, ns = ctx.doc, ctx.doc.nsmap
    ref = "DTVP IT-6 (Service Provider „M“)"
    for s in doc.services:
        kw = _kw(s)
        if not doc.text(s.el, "d:ProviderName"):
            f.error("DE-M-ProviderName", "ProviderName fehlt", ref=ref, **kw)
        if s.service_type is None:
            f.error("DE-M-ServiceType", "ServiceType fehlt", ref=ref, **kw)
        if s.el.find("d:ContentGuideSourceRef", ns) is None and s.el.find("d:ContentGuideSource", ns) is None:
            f.error("DE-M-CGSourceRef", "ContentGuideSourceRef fehlt", ref=ref, **kw)
        if s.el.find("d:ContentGuideServiceRef", ns) is None:
            f.error("DE-M-CGServiceRef", "ContentGuideServiceRef fehlt (Client nutzt ersatzweise den UniqueIdentifier)",
                    ref=ref, **kw)
        if not s.el.get("version"):
            f.error("DE-M-version", "@version fehlt", ref=ref, **kw)
        if not doc.logo_uris(s):
            f.error("DE-M-Logo", "Service-Logo fehlt", ref=ref, **kw)
        for i in s.el.findall("d:ServiceInstance", ns):
            if i.find("d:DisplayName", ns) is None:
                f.error("DE-M-DisplayName", "ServiceInstance ohne DisplayName", ref=ref,
                        detail=f"priority {i.get('priority')}", **kw)
            if not i.get("priority"):
                f.error("DE-M-priority", "ServiceInstance ohne @priority", ref=ref, **kw)
    if not doc.xr("//d:LCNRange"):
        f.error("DE-M-LCNRange", "Kein LCNRange vorhanden (für DVB-Overscan benötigt)", ref="DTVP IT-6 / IT-9")
    for idx, t in enumerate(doc.lcn_tables, 1):
        if t.find("d:TargetRegion", ns) is None:
            f.warning("DE-M-TargetRegion", "LCNTable ohne TargetRegion", ref=ref,
                      subject=doc.table_label(idx, t), detail="als bundesweite Default-Tabelle plausibel")


# --------------------------------------------------------------------------- Book 7.3, IT-11..14
def _delivery(ctx: Context, f: Findings) -> None:
    doc, ns = ctx.doc, ctx.doc.nsmap
    for s in doc.services:
        kw = _kw(s)
        insts = s.el.findall("d:ServiceInstance", ns)
        broadband = native_bb = app12 = False
        for i in insts:
            p = i.find("d:DVBSDeliveryParameters", ns)
            if p is not None:
                miss = [e for e in ("OrbitalPosition", "Frequency", "Polarization", "SymbolRate")
                        if p.find(f"d:{e}", ns) is None]
                if miss:
                    f.error("DE-SAT", "DVB-S/S2-Instanz ohne vollständige Direct-Tuning-Parameter",
                            ref="Book 7.3 („shall“), DTVP IT-13",
                            detail=f"priority {i.get('priority')}: fehlt {', '.join(miss)}", **kw)
            p = i.find("d:DVBTDeliveryParameters", ns)
            if p is not None:
                tr = p.find("d:DVBTriplet", ns)
                if tr is not None and tr.get("origNetId") is None:
                    f.warning("DE-DVBT", "DVB-T-Triplet ohne origNetId (Matching über serviceId + origNetId)",
                              ref="DTVP IT-6 / IT-11", **kw)
            if i.find("d:DASHDeliveryParameters", ns) is not None:
                broadband = native_bb = True
                if i.find("d:ContentProtection/d:DRMSystemId", ns) is not None:
                    f.error("DE-DRM", "DRM-geschützte DASH-Instanz ohne Linked App 1.2",
                            ref="Book 11.3 („shall“)", **kw)
            if i.find("d:IdentifierBasedDeliveryParameters", ns) is not None:
                broadband = native_bb = True
            if i.find("d:OtherDeliveryParameters", ns) is not None:
                broadband = True
            if "1.2" in doc.linked_apps(i):
                broadband = app12 = True
        if "end-of-service-list" in s.uid:
            continue
        if not broadband:
            f.error("DE-BB", "Keine Broadband-Instanz (DASH, Linked App 1.2 oder Icecast)",
                    ref="Book 7.3 („shall“)", **kw)
        elif not (native_bb or app12):
            f.error("DE-HLS", "Broadband nur als natives HLS (ohne DASH bzw. Linked App 1.2)",
                    ref="DTVP IT-6 (OtherDeliveryParameters)", **kw)
        if s.service_type == "linear-radio" and not broadband:
            f.error("DE-RADIO-IP", "Radio ohne IP-Stream (DASH/Icecast/Linked App 1.2)", ref="Book 7.4", **kw)


# --------------------------------------------------------------------------- Book 12, IT-17
def _content_guide_proxy(ctx: Context, f: Findings) -> None:
    doc = ctx.doc
    hosts: dict[str, set[str]] = defaultdict(set)
    for c in doc.xr("//d:ContentGuideSource"):
        for u in c.iterfind(".//ty:URI", doc.nsmap):
            h = re.sub(r"^https?://([^/:]+).*$", r"\1", (u.text or "").strip())
            hosts[h].add(c.get("CGSID") or "(inline)")
    allowed = [h.lower() for h in ctx.cfg.cg_proxy_hosts]
    for h, ids in sorted(hosts.items()):
        if allowed:
            if not any(h.lower() == a or h.lower().endswith("." + a) for a in allowed):
                f.error("DE-CG-PROXY", "Content-Guide-Endpoint nicht über den Proxy des Service List Providers",
                        ref="Book 12 („shall“), DTVP IT-17", subject=h, detail="CGSID " + ", ".join(sorted(ids)))
        else:
            f.info("DE-CG-HOST", "Content-Guide-Host (Proxy-Pflicht prüfen; erlaubte Hosts per "
                                 "cg_proxy_hosts konfigurieren)", ref="Book 12, DTVP IT-17",
                   subject=h, detail="CGSID " + ", ".join(sorted(ids)))


# --------------------------------------------------------------------------- Book 6.3 / 6.6
def _match(pattern: str, s: Service | None) -> bool:
    return bool(s) and bool(re.search(pattern, f"{s.uid}\n{s.name}\n{s.provider}", re.M))


def _reference_table(doc: ServiceListDoc):
    """Bundesweite Referenztabelle: TargetRegion = oberste Region, sonst Tabelle ohne TargetRegion."""
    tops = {rid for rid, r in doc.regions.items() if doc.region_depth(r) == 1}
    fallback = None
    for t in doc.lcn_tables:
        trg = [x.text for x in t.findall("d:TargetRegion", doc.nsmap)]
        if trg and set(trg) <= tops:
            return t
        if not trg and fallback is None:
            fallback = t
    return fallback


def _lcn_map(doc: ServiceListDoc, t) -> dict[int, str]:
    return {int(l.get("channelNumber")): l.get("serviceRef") for l in t.findall("d:LCN", doc.nsmap)}


def _lcn_plan(ctx: Context, f: Findings) -> dict:
    doc, cfg = ctx.doc, ctx.cfg
    svc = doc.by_uid.get
    end_uids = {s.uid for s in doc.services if "end-of-service-list" in s.uid}
    ref_t = _reference_table(doc)
    ref_m = _lcn_map(doc, ref_t) if ref_t is not None else {}
    block_use = Counter()
    distinct = set()
    for idx, t in enumerate(doc.lcn_tables, 1):
        label = doc.table_label(idx, t)
        m = _lcn_map(doc, t)
        distinct.update(m)
        if len(m) > cfg.lcn_limit:
            f.error("DE-LCN-LIMIT", f"Mehr als {cfg.lcn_limit} LCNs in einer Tabelle", ref="Book 6.3",
                    subject=label, detail=str(len(m)))
        by_svc = defaultdict(list)
        for n, u in m.items():
            block_use[block_of(n)] += 1
            by_svc[u].append(n)
            s = svc(u)
            if 3000 <= n <= 3998:
                f.error("DE-LCN-RES", "LCN im reservierten Block 3000–3998", ref="Book 6.6",
                        subject=label, detail=f"LCN {n} → {s.name if s else u}")
            if s is None:
                continue
            if s.service_type == "linear-radio" and not 2500 <= n <= 2999:
                f.error("DE-LCN-RADIO", "Radio-Service außerhalb des Radio-Blocks 2500–2999",
                        ref="Book 6.6 / 7.4", service=s.uid, subject=s.name, detail=f"{label}, LCN {n}")
            if 2500 <= n <= 2999 and s.service_type != "linear-radio":
                f.error("DE-LCN-RTYPE", "Service im Radio-Block nicht als linear-radio signalisiert",
                        ref="Book 7.4 („signalled as service type radio“)", service=s.uid, subject=s.name,
                        detail=f"{label}, LCN {n}, ServiceType {s.service_type or 'fehlt'}")
            if n <= 2499 and s.service_type == "linear-radio":
                pass  # schon über DE-LCN-RADIO erfasst
        # Endkanal
        if 3999 not in m:
            f.error("DE-LCN-END", "Kein End-of-Service-List-Kanal auf LCN 3999", ref="Book 6.3 / 6.6", subject=label)
        elif m[3999] not in end_uids:
            f.error("DE-LCN-END", "LCN 3999 nicht mit dem End-of-Service-List-Kanal belegt", ref="Book 6.6",
                    subject=label, detail=svc(m[3999]).name if svc(m[3999]) else m[3999])
        # ein Service – eine LCN
        for u, lcns in by_svc.items():
            if len(lcns) > 1:
                s = svc(u)
                f.error("DE-LCN-DUP", "Service auf mehreren LCNs derselben Tabelle",
                        ref="Book 6.6 („all reception technologies … within a single LCN“)",
                        service=u, subject=s.name if s else u,
                        detail=f"{label}: LCN {', '.join(map(str, sorted(lcns)))}")
        # LCN 3/4 bzw. 50/51 (Book 6.6, Block 1)
        for slot, alt, pat, who in ((3, 50, cfg.pattern_rtl, "RTL"), (4, 51, cfg.pattern_sat1, "SAT.1")):
            if slot in m and not _match(pat, svc(m[slot])):
                f.warning("DE-LCN-34", f"LCN {slot} ist nicht mit {who} (national oder Regionalfenster) belegt",
                          ref="Book 6.6 Block 1", subject=label, detail=svc(m[slot]).name if svc(m[slot]) else m[slot])
            if alt in m and slot in m and ref_m.get(slot) == m[slot]:
                f.error("DE-LCN-5051", f"LCN {alt} belegt, obwohl die nationale {who}-Version auf LCN {slot} liegt",
                        ref="Book 6.6 (LCN 50/51)", subject=label)
        # LCN 7 und Block 41–49
        if 7 in m:
            s7 = svc(m[7])
            if not _match(cfg.pattern_ard, s7):
                f.warning("DE-LCN-7", "LCN 7 nicht mit einem ARD-Regionalprogramm belegt", ref="Book 6.6 Block 1",
                          subject=label, detail=s7.name if s7 else m[7])
            dup = [n for n in range(41, 50) if m.get(n) == m[7]]
            if dup:
                f.error("DE-LCN-ARD7", "Regionalprogramm von LCN 7 zusätzlich im Block 41–49 (Platz bleibt laut Book leer)",
                        ref="Book 6.6 Block 1", service=m[7], subject=label,
                        detail=f"{s7.name if s7 else m[7]} auf LCN 7 und {dup[0]}")
            if ref_m and t is not ref_t:
                empty = [n for n in range(41, 50) if n in ref_m and n not in m]
                if len(empty) == 1 and ref_m[empty[0]] != m[7]:
                    exp = svc(ref_m[empty[0]])
                    f.error("DE-LCN-7MAP", "LCN 7 passt nicht zum ausgelassenen Hauptprogramm im Block 41–49",
                            ref="Book 6.6 Block 1", service=m[7], subject=label,
                            detail=f"LCN 7 = {s7.name if s7 else m[7]}, frei gelassen: LCN {empty[0]} "
                                   f"({exp.name if exp else ref_m[empty[0]]}) – erwartet wäre dieses Programm auf LCN 7")
        for n in range(41, 50):
            if n in m and not _match(cfg.pattern_ard, svc(m[n])):
                f.warning("DE-LCN-41", "Block 41–49 enthält kein ARD-Hauptprogramm", ref="Book 6.6 Block 1",
                          subject=label, detail=f"LCN {n}: {svc(m[n]).name if svc(m[n]) else m[n]}")
        # Block 6: nur Sender, die nicht schon in einem früheren Block stehen
        early = {u for n, u in m.items() if n < 1000}
        for n, u in m.items():
            if 1000 <= n <= 1499 and u in early:
                s = svc(u)
                f.error("DE-LCN-B6", "Service im Regional-Block 6, obwohl bereits in einem früheren Block gelistet",
                        ref="Book 6.6 Block 6", service=u, subject=s.name if s else u, detail=f"{label}, LCN {n}")
        # Unterblöcke 6A–6C
        for a, b, pat, who in ((1000, 1018, cfg.pattern_ard, "ARD"), (1019, 1024, cfg.pattern_rtl, "RTL"),
                               (1025, 1028, cfg.pattern_sat1, "SAT.1")):
            for n in range(a, b + 1):
                if n in m and not _match(pat, svc(m[n])):
                    f.warning("DE-LCN-B6SUB", f"LCN {a}–{b} ist für {who}-Regionalprogramme reserviert",
                              ref="Book 6.6 Block 6", subject=label,
                              detail=f"LCN {n}: {svc(m[n]).name if svc(m[n]) else m[n]}")
    n_distinct = len(distinct)
    if n_distinct > cfg.lcn_notify_threshold:
        f.warning("DE-LCN-900", f"{n_distinct} LCNs vergeben – über der Meldeschwelle {cfg.lcn_notify_threshold}",
                  ref="Book 6.3")
    f.info("DE-LCN-COUNT", f"{n_distinct} verschiedene LCNs vergeben (Grenze {cfg.lcn_limit})", ref="Book 6.3")
    return {"lcn_distinct": n_distinct, "lcn_block_usage": dict(block_use),
            "reference_table": doc.table_label(doc.lcn_tables.index(ref_t) + 1, ref_t) if ref_t is not None else None}


# --------------------------------------------------------------------------- Book 6.9, IT-4
def _popups(ctx: Context, f: Findings) -> None:
    doc, ns = ctx.doc, ctx.doc.nsmap
    for l in doc.xr("//d:LCN[@selectable or @visible]"):
        f.error("DE-POPUP-FLAG", "@selectable/@visible an LCN verwendet", ref="Book 6.9 („shall not … for the time being“)",
                detail=f"LCN {l.get('channelNumber')}")
    for a in doc.xr("//d:Availability"):
        svc = doc.service_of(a)
        f.error("DE-POPUP-AVAIL", "Availability-Intervall verwendet", ref="Book 6.9 („shall not … for the time being“)",
                service=svc.uid if svc else None, subject=svc.name if svc else "")
    popup = {l.get("serviceRef") for l in doc.xr("//d:LCN") if 1500 <= int(l.get("channelNumber")) <= 1999}
    for u in sorted(popup):
        s = doc.by_uid.get(u)
        if s is None:
            continue
        hrefs = [h.get("href", "") for h in s.el.iterfind(".//t:HowRelated", ns)]
        if not any(OOS_BANNER_HOWRELATED.search(h) or OOS_APP.search(h) for h in hrefs):
            f.error("DE-POPUP-OOS", "Pop-up-Kanal ohne Out-of-Service-Banner bzw. -App",
                    ref="Book 6.9, DTVP IT-6 (Out of Service Banner „M“)", **_kw(s))
    providers = Counter(doc.by_uid[u].provider for u in popup if u in doc.by_uid)
    for p, n in providers.items():
        f.info("DE-POPUP-COUNT", f"{n} Pop-up-Kanäle", ref="Book 6.9 (Kontingent nach AGF-Marktanteil)",
               subject=p or "(ohne Anbieter)", detail="Kontingent mangels AGF-Daten nicht prüfbar")


# --------------------------------------------------------------------------- Book 10.2, IT-5
def _regions(ctx: Context, f: Findings) -> None:
    doc = ctx.doc
    levels = Counter(doc.region_depth(r) for r in doc.regions.values())
    for rid, r in doc.regions.items():
        if doc.region_depth(r) > 4:
            f.error("DE-REG-DEPTH", "Mehr als Land + 3 Subregionen", ref="Book 10.2 / DTVP IT-5", subject=rid)
    if doc.regions:
        f.info("DE-REG-LEVELS", "Regionen je Ebene: " + ", ".join(f"Ebene {k}: {v}" for k, v in sorted(levels.items())),
               ref="Book 10.2")


# --------------------------------------------------------------------------- IT-5, IT-18 (Server)
def _server_features(ctx: Context, f: Findings) -> None:
    base, client, cfg = ctx.source.location, ctx.client, ctx.cfg
    # IT-5: ungültige PLZ -> bundesweite Liste mit responseStatus ERROR_INVALID_POSTCODE
    r = client.get(add_query(base, postcode=cfg.invalid_postcode), use_cache=False)
    if not r.ok:
        f.warning("DE-SRV-PLZ-INV", "Abfrage mit ungültiger PLZ liefert keine Liste", ref="DTVP IT-5",
                  detail=r.describe())
    else:
        try:
            root = etree.fromstring(r.content, etree.XMLParser(no_network=True, resolve_entities=False, huge_tree=True))
            status = root.get("responseStatus")
            if status != "ERROR_INVALID_POSTCODE":
                f.error("DE-SRV-PLZ-INV", "Ungültige PLZ wird nicht mit responseStatus=ERROR_INVALID_POSTCODE beantwortet",
                        ref="DTVP IT-5", detail=f"responseStatus={status!r} für postcode={cfg.invalid_postcode}")
        except etree.XMLSyntaxError as e:
            f.error("DE-SRV-PLZ-INV", "Antwort auf ungültige PLZ ist kein XML", ref="DTVP IT-5", detail=str(e))
    # IT-5: gültige PLZ -> Liste ohne Regionsdefinitionen
    r = client.get(add_query(base, postcode=cfg.server_test_postcode), use_cache=False)
    if r.ok:
        try:
            root = etree.fromstring(r.content, etree.XMLParser(no_network=True, resolve_entities=False, huge_tree=True))
            if any(local(c) == "RegionList" for c in root):
                f.warning("DE-SRV-PLZ", "Liste für gültige PLZ enthält noch eine RegionList",
                          ref="DTVP IT-5", detail=f"postcode={cfg.server_test_postcode}")
            else:
                f.info("DE-SRV-PLZ", "Serverseitige Regionalisierung liefert Liste ohne RegionList",
                       ref="DTVP IT-5", detail=f"postcode={cfg.server_test_postcode}, {len(r.content)} Bytes")
        except etree.XMLSyntaxError as e:
            f.error("DE-SRV-PLZ", "Antwort auf gültige PLZ ist kein XML", ref="DTVP IT-5", detail=str(e))
    else:
        f.warning("DE-SRV-PLZ", "Abfrage mit gültiger PLZ fehlgeschlagen", ref="DTVP IT-5", detail=r.describe())
    # IT-18: Conditional GET
    meta = ctx.source.meta
    hdrs = {}
    if meta.get("etag"):
        hdrs["If-None-Match"] = meta["etag"]
    hdrs["If-Modified-Since"] = meta.get("last_modified") or format_datetime(datetime.now(timezone.utc), usegmt=True)
    r = client.get(ctx.source.request_url or base, headers=hdrs, use_cache=False)
    if r.status == 304:
        f.info("DE-SRV-IMS", "Conditional GET wird mit 304 Not Modified beantwortet", ref="DTVP IT-18")
    else:
        f.warning("DE-SRV-IMS", "Conditional GET (If-Modified-Since/ETag) liefert kein 304", ref="DTVP IT-18",
                  detail=f"{r.describe()}; Last-Modified={meta.get('last_modified')}, ETag={meta.get('etag')}, "
                         f"Cache-Control={meta.get('cache_control')}")
