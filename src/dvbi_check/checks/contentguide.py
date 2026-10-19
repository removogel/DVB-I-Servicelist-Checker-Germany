"""Prüfung der Content-Guide-Endpunkte (TV-Anytime): Now/Next, Zeitfenster, Programminformation.

Abfragen nach A177 Kap. 6 (wie im DVB-I-Referenzvalidator):
  ScheduleInfoEndpoint?sid=<id>&now_next=true
  ScheduleInfoEndpoint?sid=<id>&start=<epoch>&end=<epoch>
  ProgramInfoEndpoint?pid=<crid>
Inhaltliche Anforderungen: DTVP IT-6 (Content Guide Metadata, Tabellen 41–64).
"""
from __future__ import annotations

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from lxml import etree

from ..httpclient import HttpResult, add_query
from ..model import Finding, Findings, ModuleResult, Severity, Timer
from ..resources import load_schema
from ..sl import Service, TVA_PREFIX
from . import Context
from .images import check_image

NOW_CRID = "crid://dvb.org/metadata/schedules/now-next/now"
LATER_CRID = "crid://dvb.org/metadata/schedules/now-next/later"
DUR_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")
REF = "DTVP IT-6 (Content Guide Metadata)"
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


# ------------------------------------------------------------------ helpers
def parse_duration(s: str | None) -> timedelta | None:
    if not s:
        return None
    m = DUR_RE.match(s.strip())
    if not m or s.strip() in ("P", "PT"):
        return None
    d, h, mi, se = m.groups()
    return timedelta(days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=float(se or 0))


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def kid(el, name):
    return el.find("{*}" + name) if el is not None else None


def kids(el, name):
    return el.findall("{*}" + name) if el is not None else []


def txt(el, name) -> str | None:
    k = kid(el, name)
    return k.text.strip() if k is not None and k.text and k.text.strip() else None


@dataclass
class Event:
    crid: str | None
    start: datetime | None
    duration: timedelta | None
    has_instance_desc: bool

    @property
    def end(self):
        return self.start + self.duration if self.start and self.duration else None


@dataclass
class TvaDoc:
    root: etree._Element
    ns: str
    programs: dict[str, etree._Element] = field(default_factory=dict)
    schedules: list[etree._Element] = field(default_factory=list)
    groups: list[etree._Element] = field(default_factory=list)


class ServiceCG:
    """Sammelt Befunde eines Service und fasst gleichartige Mängel zusammen."""

    def __init__(self, svc: Service):
        self.svc = svc
        self.findings: list[Finding] = []
        self.counters: Counter = Counter()
        self.stats: dict = {"service": svc.uid, "name": svc.name}

    def add(self, code, sev, msg, ref=REF, detail=""):
        self.findings.append(Finding("cg", code, sev, msg, ref, self.svc.uid, self.svc.name, detail))


# ------------------------------------------------------------------ response handling
def fetch_tva(ctx: Context, sc: ServiceCG, url: str, label: str, where: str = "") -> TvaDoc | None:
    pre = f"{where} · " if where else ""
    r: HttpResult = ctx.client.get(url)
    sc.stats.setdefault("requests", 0)
    sc.stats["requests"] += 1
    sc.stats.setdefault("response_times", []).append(round(r.elapsed, 3))
    if not r.ok:
        sc.add("CG-HTTP", Severity.ERROR, f"{label}: Endpunkt antwortet nicht erfolgreich", detail=f"{pre}{r.describe()} · {url}")
        return None
    if r.elapsed > ctx.cfg.cg_slow_seconds:
        sc.counters[("CG-SLOW", label)] += 1
    if "xml" not in r.content_type:
        sc.add("CG-CT", Severity.WARNING, f"{label}: Content-Type ist kein XML", detail=f"{pre}{r.content_type} · {url}")
    try:
        root = etree.fromstring(r.content, etree.XMLParser(no_network=True, resolve_entities=False, huge_tree=True))
    except etree.XMLSyntaxError as e:
        sc.add("CG-XML", Severity.ERROR, f"{label}: Antwort ist kein wohlgeformtes XML", detail=f"{pre}{e} · {url}")
        return None
    q = etree.QName(root)
    if q.localname != "TVAMain" or not (q.namespace or "").startswith(TVA_PREFIX):
        sc.add("CG-ROOT", Severity.ERROR, f"{label}: Root-Element ist nicht TVAMain (TV-Anytime)",
               detail=f"{pre}{root.tag} · {url}")
        return None
    doc = TvaDoc(root, q.namespace)
    # XSD
    if ctx.res is not None:
        xsd = ctx.res.tva_schema_file(q.namespace.rsplit(":", 1)[-1])
        if xsd is None:
            sc.add("CG-XSD", Severity.WARNING, f"{label}: Kein TVA-Schema für {q.namespace}", detail=url)
        else:
            schema = load_schema(str(xsd))
            if not schema.validate(root.getroottree()):
                msgs = Counter(re.sub(r"\{[^}]*\}", "", e.message) for e in schema.error_log)
                for m, n in list(msgs.items())[:3]:
                    sc.add("CG-XSD", Severity.ERROR, f"{label}: Schemafehler (TV-Anytime)",
                           ref=f"TVA {xsd.name}", detail=f"{pre}{m} ({n}×) · {url}")
    pd = kid(root, "ProgramDescription")
    for pi in kids(kid(pd, "ProgramInformationTable"), "ProgramInformation"):
        doc.programs[pi.get("programId")] = pi
    doc.schedules = kids(kid(pd, "ProgramLocationTable"), "Schedule")
    doc.groups = kids(kid(pd, "GroupInformationTable"), "GroupInformation")
    return doc


def events_of(schedule) -> list[Event]:
    out = []
    for ev in kids(schedule, "ScheduleEvent"):
        p = kid(ev, "Program")
        out.append(Event(p.get("crid") if p is not None else None,
                         parse_time(txt(ev, "PublishedStartTime")),
                         parse_duration(txt(ev, "PublishedDuration")),
                         kid(ev, "InstanceDescription") is not None))
    return out


def check_program_info(sc: ServiceCG, pi, where: str) -> list[str]:
    """Prüft ProgramInformation (Tab. 41–43) und liefert Bild-URLs."""
    bd = kid(pi, "BasicDescription")
    if bd is None:
        sc.counters[("CG-PI-BD", where)] += 1
        return []
    if not txt(bd, "Title"):
        sc.counters[("CG-PI-TITLE", where)] += 1
    if not txt(bd, "Synopsis"):
        sc.counters[("CG-PI-SYN", where)] += 1
    images = []
    for rm in kids(bd, "RelatedMaterial"):
        for mu in rm.iter("{*}MediaUri"):
            ct = (mu.get("contentType") or "").lower()
            if mu.text and (ct.startswith("image/") or re.search(r"\.(jpe?g|png|webp)(\?|$)", mu.text, re.I)):
                images.append((mu.text.strip(), ct))
    if not images:
        sc.counters[("CG-PI-IMG", where)] += 1
    pgs = kids(bd, "ParentalGuidance")
    if pgs and kid(pgs[0], "MinimumAge") is None:
        sc.counters[("CG-PI-AGE", where)] += 1
    credits = kid(bd, "CreditsList")
    if credits is not None and len(kids(credits, "CreditsItem")) > 40:
        sc.counters[("CG-PI-CREDITS", where)] += 1
    sc.counters[("CG-PI-TOTAL", where)] += 1
    return images


# ------------------------------------------------------------------ per request type
def check_now_next(ctx, sc, ep, sid, now) -> tuple[list[str], list]:
    url = add_query(ep, sid=sid, now_next="true")
    doc = fetch_tva(ctx, sc, url, "Now/Next")
    if doc is None:
        sc.stats["now_next"] = "fehlerhaft"
        return [], []
    groups = {g.get("groupId"): g for g in doc.groups}
    if not doc.groups:
        sc.add("CG-NN-GROUP", Severity.ERROR, "Now/Next: GroupInformationTable fehlt", detail=url)
    for crid in (NOW_CRID, LATER_CRID):
        g = groups.get(crid)
        if doc.groups and g is None:
            sc.add("CG-NN-GROUP", Severity.WARNING, f"Now/Next: GroupInformation {crid.rsplit('/', 1)[-1]} fehlt", detail=url)
        elif g is not None:
            for a in ("ordered", "numOfItems"):
                if g.get(a) is None:
                    sc.add("CG-NN-GROUP", Severity.ERROR, f"Now/Next: GroupInformation@{a} fehlt",
                           ref="DTVP IT-6 (Tab. 64)", detail=crid)
            if kid(g, "GroupType") is None or kid(g, "BasicDescription") is None:
                sc.add("CG-NN-GROUP", Severity.ERROR, "Now/Next: GroupType/BasicDescription fehlt",
                       ref="DTVP IT-6 (Tab. 64)", detail=crid)
            n = g.get("numOfItems")
            if n is not None and (not n.isdigit() or not 0 < int(n) <= 1):
                sc.add("CG-NN-GROUP", Severity.ERROR, "Now/Next: numOfItems muss 1 sein", detail=f"{crid} = {n}")
    for g in doc.groups:
        if g.get("groupId") not in (NOW_CRID, LATER_CRID):
            sc.add("CG-NN-GROUP", Severity.WARNING, "Now/Next: unerwartete GroupInformation", detail=g.get("groupId"))
    events = []
    for s in doc.schedules:
        if s.get("serviceIDRef") != sid:
            sc.add("CG-SID", Severity.ERROR, "Now/Next: Schedule@serviceIDRef passt nicht zur angefragten sid",
                   detail=f"{s.get('serviceIDRef')} ≠ {sid}")
        events += events_of(s)
    if not events:
        sc.add("CG-NN-EMPTY", Severity.ERROR, "Now/Next: keine Events geliefert", detail=url)
        sc.stats["now_next"] = "leer"
        return [], []
    if len(events) > 2:
        sc.add("CG-NN-COUNT", Severity.WARNING, f"Now/Next: {len(events)} Events statt höchstens 2", detail=url)
    running = [e for e in events if e.start and e.end and e.start <= now < e.end]
    if not running:
        sc.add("CG-NN-NOW", Severity.WARNING, "Now/Next: kein Event deckt die aktuelle Uhrzeit ab",
               detail=", ".join(f"{e.start:%H:%M}–{e.end:%H:%M}" for e in events if e.start and e.end) + f" · {url}")
    sc.stats["now_next"] = "ok" if running else "ohne laufendes Event"
    sc.stats["now_title"] = None
    images = []
    for e in events:
        _event_fields(sc, e, "Now/Next")
        pi = doc.programs.get(e.crid)
        if pi is None:
            sc.counters[("CG-PI-MISSING", "Now/Next")] += 1
            continue
        images += check_program_info(sc, pi, "Now/Next")
        if e in running and sc.stats["now_title"] is None:
            sc.stats["now_title"] = txt(kid(pi, "BasicDescription"), "Title")
    crids = [e.crid for e in (running or events) if e.crid]
    return crids, images


def _event_fields(sc: ServiceCG, e: Event, where: str) -> None:
    sc.counters[("CG-EV-TOTAL", where)] += 1
    if not e.crid:
        sc.counters[("CG-EV-CRID", where)] += 1
    if not e.start:
        sc.counters[("CG-EV-START", where)] += 1
    if not e.duration:
        sc.counters[("CG-EV-DUR", where)] += 1
    if not e.has_instance_desc:
        sc.counters[("CG-EV-INSTDESC", where)] += 1


def check_schedule_day(ctx, sc, ep, sid, day_start: datetime, offset: int) -> tuple[list[str], list]:
    day_end = day_start + timedelta(days=1)
    url = add_query(ep, sid=sid, start=int(day_start.timestamp()), end=int(day_end.timestamp()))
    label = "Zeitfenster"
    dlabel = f"{WEEKDAYS[day_start.weekday()]} {day_start:%d.%m.}"
    doc = fetch_tva(ctx, sc, url, label, dlabel)
    days = sc.stats.setdefault("days", {})
    if doc is None:
        days[offset] = None
        return [], []
    events = []
    for s in doc.schedules:
        for a in ("serviceIDRef", "start", "end"):
            if s.get(a) is None:
                sc.counters[("CG-SCHED-ATTR", a)] += 1
        if s.get("serviceIDRef") not in (None, sid):
            sc.add("CG-SID", Severity.ERROR, f"{label}: Schedule@serviceIDRef passt nicht zur angefragten sid",
                   detail=f"{dlabel} · {s.get('serviceIDRef')} ≠ {sid}")
        events += events_of(s)
    for e in events:
        _event_fields(sc, e, "Zeitfenster")
    iv = sorted((max(e.start, day_start), min(e.end, day_end)) for e in events
                if e.start and e.end and e.end > day_start and e.start < day_end)
    covered, gaps, overlaps, cursor = timedelta(), [], 0, day_start
    for a, b in iv:
        if a > cursor and (a - cursor) > timedelta(minutes=ctx.cfg.cg_max_gap_minutes):
            gaps.append((cursor, a))
        if a < cursor:
            overlaps += 1
        if b > cursor:
            covered += b - max(a, cursor)
            cursor = b
    if day_end - cursor > timedelta(minutes=ctx.cfg.cg_max_gap_minutes) and iv:
        gaps.append((cursor, day_end))
    coverage = covered / (day_end - day_start)
    days[offset] = round(coverage, 3)
    must = offset >= 0  # heute + 7 Tage „shall“, Vergangenheit „should“
    if not events:
        sc.add("CG-DAY-EMPTY", Severity.ERROR if must else Severity.WARNING,
               "Zeitfenster: keine Programmdaten für " + ("heute bzw. einen kommenden Tag" if must else "einen vergangenen Tag"),
               ref="DTVP IT-6 (heute + 7 Tage „shall“, 7 Tage zurück „should“)", detail=f"{dlabel} · {url}")
    elif coverage < ctx.cfg.cg_min_coverage:
        g = ", ".join(f"{a.astimezone(day_start.tzinfo):%H:%M}–{b.astimezone(day_start.tzinfo):%H:%M}" for a, b in gaps[:4])
        sc.add("CG-DAY-GAPS", Severity.WARNING, f"Zeitfenster: Tag nicht vollständig abgedeckt (< {ctx.cfg.cg_min_coverage:.0%})",
               detail=f"{dlabel}: {coverage:.0%}, Lücken {g} · {url}")
    if overlaps:
        sc.add("CG-DAY-OVERLAP", Severity.WARNING, "Zeitfenster: überlappende Events", detail=f"{dlabel}: {overlaps}× · {url}")
    images = []
    for e in events:
        pi = doc.programs.get(e.crid)
        if pi is not None:
            images += check_program_info(sc, pi, "Zeitfenster")
        elif e.crid:
            sc.counters[("CG-PI-INLINE", "Zeitfenster")] += 1
    return [e.crid for e in events if e.crid][:3], images


def check_program_endpoint(ctx, sc, ep, crid) -> list:
    url = add_query(ep, pid=crid)
    doc = fetch_tva(ctx, sc, url, "Programminfo")
    if doc is None:
        sc.stats["program_info"] = "fehlerhaft"
        return []
    pi = doc.programs.get(crid)
    if pi is None:
        sc.add("CG-PROG-MISSING", Severity.ERROR, "Programminfo: angefragte programId fehlt in der Antwort",
               detail=f"{crid} · {url}")
        sc.stats["program_info"] = "ohne angefragtes Programm"
        return []
    sc.stats["program_info"] = "ok"
    return check_program_info(sc, pi, "Programminfo")


# ------------------------------------------------------------------ aggregation
COUNTER_TEXT = {
    "CG-PI-BD": (Severity.ERROR, "ProgramInformation ohne BasicDescription", "DTVP IT-6 (Tab. 41)"),
    "CG-PI-TITLE": (Severity.ERROR, "Programm ohne Title", "DTVP IT-6 (Tab. 42/43)"),
    "CG-PI-SYN": (Severity.ERROR, "Programm ohne Synopsis", "DTVP IT-6 (Tab. 42/43)"),
    "CG-PI-IMG": (Severity.ERROR, "Programm ohne Bild (RelatedMaterial)", "DTVP IT-6 (Tab. 42/43, Service Provider „M“)"),
    "CG-PI-AGE": (Severity.ERROR, "Erstes ParentalGuidance ohne MinimumAge", "DTVP IT-6 (Tab. 61)"),
    "CG-PI-CREDITS": (Severity.WARNING, "CreditsList mit mehr als 40 Einträgen", "DTVP IT-6 (Tab. 43)"),
    "CG-PI-MISSING": (Severity.ERROR, "Event ohne zugehörige ProgramInformation", "A177 / DTVP IT-6"),
    "CG-PI-INLINE": (Severity.INFO, "Event ohne ProgramInformation in derselben Antwort", "A177"),
    "CG-EV-CRID": (Severity.ERROR, "ScheduleEvent ohne Program@crid", "DTVP IT-6 (Tab. 51)"),
    "CG-EV-START": (Severity.ERROR, "ScheduleEvent ohne PublishedStartTime", "DTVP IT-6 (Tab. 51)"),
    "CG-EV-DUR": (Severity.ERROR, "ScheduleEvent ohne PublishedDuration", "DTVP IT-6 (Tab. 51)"),
    "CG-EV-INSTDESC": (Severity.ERROR, "ScheduleEvent ohne InstanceDescription", "DTVP IT-6 (Tab. 51, Service Provider „M“)"),
    "CG-SCHED-ATTR": (Severity.ERROR, "Schedule ohne Pflichtattribut", "DTVP IT-6 (Tab. 50)"),
    "CG-SLOW": (Severity.WARNING, "Langsame Antworten", "Leistung"),
}


def flush_counters(sc: ServiceCG, cfg) -> None:
    totals = {where: n for (code, where), n in sc.counters.items() if code in ("CG-PI-TOTAL", "CG-EV-TOTAL")}
    for (code, where), n in sorted(sc.counters.items()):
        if code in ("CG-PI-TOTAL", "CG-EV-TOTAL"):
            continue
        sev, msg, ref = COUNTER_TEXT[code]
        if code == "CG-SCHED-ATTR":
            sc.add(code, sev, f"{msg} @{where}", ref, f"{n}×")
        elif code == "CG-SLOW":
            sc.add(code, sev, f"{where}: {n} Antwort(en) langsamer als {cfg.cg_slow_seconds} s", ref)
        else:
            base = sc.counters.get(("CG-EV-TOTAL" if code.startswith("CG-EV") else "CG-PI-TOTAL", where))
            sc.add(code, sev, f"{where}: {msg}", ref, f"{n} von {base or '?'}")


# ------------------------------------------------------------------ module
def check_service(ctx: Context, svc: Service) -> ServiceCG:
    sc = ServiceCG(svc)
    doc = ctx.doc
    cgs = doc.cg_source_for(svc)
    if cgs is None:
        sc.add("CG-NOSOURCE", Severity.ERROR, "Keine Content-Guide-Quelle – kein EPG und kein Now/Next",
               ref="DTVP IT-6 (ContentGuideSourceRef „M“)")
        sc.stats["now_next"] = "keine Quelle"
        return sc
    sid = doc.cg_service_id(svc)
    sc.stats["sid"] = sid
    sched = doc.endpoint(cgs, "ScheduleInfoEndpoint")
    prog = doc.endpoint(cgs, "ProgramInfoEndpoint")
    if not sched:
        sc.add("CG-NOEP", Severity.ERROR, "ContentGuideSource ohne ScheduleInfoEndpoint")
        return sc
    for u in (sched, prog):
        if u and u.startswith("http://"):
            sc.add("CG-HTTPS", Severity.WARNING, "Content-Guide-Endpoint ohne HTTPS", detail=u)
    now = datetime.now(timezone.utc)
    crids, images = check_now_next(ctx, sc, sched, sid, now)
    tz = ZoneInfo(ctx.cfg.cg_timezone)
    today = now.astimezone(tz).date()
    for off in range(-ctx.cfg.cg_days_past, ctx.cfg.cg_days_future + 1):
        start = datetime.combine(today + timedelta(days=off), time(0), tz)
        c, im = check_schedule_day(ctx, sc, sched, sid, start, off)
        if not crids:
            crids = c
        images += im
    if prog and crids:
        images += check_program_endpoint(ctx, sc, prog, crids[0])
    elif not prog:
        sc.stats["program_info"] = "kein Endpoint"
        sc.add("CG-NOPROG", Severity.INFO, "Kein ProgramInfoEndpoint (Detailansicht nur aus Schedule-Daten)",
               ref="DTVP IT-6 („should“ für Receiver)")
    # EPG-Bilder (Stichprobe)
    seen = set()
    sample = []
    for url, ct in images:
        if url not in seen:
            seen.add(url)
            sample.append((url, ct))
        if len(sample) >= ctx.cfg.cg_image_sample:
            break
    for url, ct in sample:
        rep = check_image(ctx.client, url, kind="epg", declared_ct=ct, cfg=ctx.cfg)
        for i in rep.issues:
            sc.add("CG-" + i.code, i.severity, "EPG-Bild: " + i.message, i.ref or REF,
                   (i.detail + " · " if i.detail else "") + url)
    flush_counters(sc, ctx.cfg)
    days = [v for v in sc.stats.get("days", {}).values() if v is not None]
    sc.stats["days_with_data"] = sum(1 for v in days if v > 0)
    sc.stats["avg_coverage"] = round(sum(days) / len(days), 3) if days else None
    rts = sc.stats.get("response_times", [])
    sc.stats["avg_response_s"] = round(sum(rts) / len(rts), 2) if rts else None
    sc.stats.pop("response_times", None)
    return sc


def run(ctx: Context) -> ModuleResult:
    res = ModuleResult("cg")
    f = Findings("cg")
    with Timer() as t:
        if ctx.offline:
            res.skipped = "Offline-Modus"
        else:
            svcs = [s for s in ctx.doc.services if "end-of-service-list" not in s.uid]
            if ctx.cfg.cg_max_services:
                svcs = svcs[: ctx.cfg.cg_max_services]
            with ThreadPoolExecutor(max_workers=ctx.cfg.workers) as ex:
                results = list(ex.map(lambda s: check_service(ctx, s), svcs))
            per_service = []
            for sc in results:
                f.extend(sc.findings)
                per_service.append(sc.stats)
            nn_ok = sum(1 for s in per_service if s.get("now_next") == "ok")
            full = sum(1 for s in per_service
                       if all((s.get("days") or {}).get(o) for o in range(0, ctx.cfg.cg_days_future + 1)))
            res.stats = {
                "services_checked": len(per_service),
                "now_next_ok": nn_ok,
                "full_future_coverage": full,
                "requests": sum(s.get("requests", 0) for s in per_service),
                "window": f"-{ctx.cfg.cg_days_past} … +{ctx.cfg.cg_days_future} Tage ({ctx.cfg.cg_timezone})",
                "per_service": per_service,
            }
    res.findings = f
    res.duration = t.elapsed
    return res
