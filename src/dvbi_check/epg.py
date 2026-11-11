"""EPG gap analysis: every content guide entry per service as an Excel workbook.

`dvbi-check epg <list>` fetches the schedules of all services the same way as the `cg` module
(A177 6.5.2.1: 3-hour aligned requests of 6 or 12 hours, merged per service), lists every
ScheduleEvent with start and end time and marks gaps and overlaps. For each entry it shows
whether the ProgramInformation provides a synopsis and a preview image.

The workbook is written in openpyxl's write-only mode, so memory stays flat even for large lists.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from lxml import etree

from .checks.contentguide import (TVA_PREFIX, events_of, kid, kids, schedule_blocks, txt)
from .config import Config
from .httpclient import HttpClient, add_query
from .loader import load
from .sl import Service, ServiceListDoc

IMAGE_RE = re.compile(r"\.(jpe?g|png|webp)(\?|$)", re.I)


@dataclass
class Entry:
    start: datetime
    end: datetime
    crid: str | None
    title: str | None = None
    synopsis: bool | None = None      # None: no ProgramInformation for this CRID
    image: bool | None = None
    status: str = "ok"                # ok | overlap
    overlap_min: float = 0.0          # overlap with the previous entries


@dataclass
class Gap:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class ServiceEpg:
    uid: str
    name: str
    lcn: int | None
    sid: str | None = None
    endpoint: str | None = None
    rows: list = field(default_factory=list)          # Entry and Gap, chronological
    errors: list[str] = field(default_factory=list)   # failed requests, missing source
    requests: int = 0

    @property
    def entries(self) -> list[Entry]:
        return [r for r in self.rows if isinstance(r, Entry)]

    def summary(self, range_start: datetime, range_end: datetime, gap_min: float) -> dict:
        es = self.entries
        gaps = [r for r in self.rows if isinstance(r, Gap)]
        big = [g for g in gaps if g.minutes > gap_min]
        span = (range_end - range_start).total_seconds()
        covered = span - sum((g.end - g.start).total_seconds() for g in gaps)
        with_pi = [e for e in es if e.synopsis is not None]
        return {
            "entries": len(es),
            "gaps": len(big),
            "gap_minutes": round(sum(g.minutes for g in big)),
            "small_gaps": len(gaps) - len(big),
            "overlaps": sum(1 for e in es if e.status == "overlap"),
            "coverage": covered / span if es and span else 0.0,
            "synopsis": sum(1 for e in es if e.synopsis) / len(es) if es else None,
            "image": sum(1 for e in es if e.image) / len(es) if es else None,
            "no_pi": len(es) - len(with_pi),
        }


# ------------------------------------------------------------------ collecting
def _program_info(pi) -> tuple[str | None, bool, bool]:
    bd = kid(pi, "BasicDescription")
    if bd is None:
        return None, False, False
    titles = kids(bd, "Title")
    main = next((t for t in titles if t.get("type", "main") == "main"), titles[0] if titles else None)
    title = (main.text or "").strip() if main is not None else None
    synopsis = any((s.text or "").strip() for s in kids(bd, "Synopsis"))
    image = False
    for rm in kids(bd, "RelatedMaterial"):
        for mu in rm.iter("{*}MediaUri"):
            ct = (mu.get("contentType") or "").lower()
            if mu.text and (ct.startswith("image/") or IMAGE_RE.search(mu.text)):
                image = True
    return title, synopsis, image


def fetch_entries(client: HttpClient, ep: str, sid: str, bs: datetime, be: datetime) -> tuple[list[Entry], str | None]:
    url = add_query(ep, sid=sid, start=int(bs.timestamp()), end=int(be.timestamp()))
    r = client.get(url, use_cache=False)
    if not r.ok:
        return [], f"{r.describe()} · {url}"
    try:
        root = etree.fromstring(r.content, etree.XMLParser(no_network=True, resolve_entities=False, huge_tree=True))
    except etree.XMLSyntaxError as e:
        return [], f"not well-formed XML ({e}) · {url}"
    q = etree.QName(root)
    if q.localname != "TVAMain" or not (q.namespace or "").startswith(TVA_PREFIX):
        return [], f"root element is not TVAMain · {url}"
    pd = kid(root, "ProgramDescription")
    programs = {pi.get("programId"): pi for pi in kids(kid(pd, "ProgramInformationTable"), "ProgramInformation")}
    out = []
    for s in kids(kid(pd, "ProgramLocationTable"), "Schedule"):
        for e in events_of(s):
            if not (e.start and e.end):
                continue
            entry = Entry(e.start, e.end, e.crid)
            pi = programs.get(e.crid)
            if pi is not None:
                entry.title, entry.synopsis, entry.image = _program_info(pi)
            else:  # title from the instance as a fallback
                for ev in kids(s, "ScheduleEvent"):
                    p = kid(ev, "Program")
                    if p is not None and p.get("crid") == e.crid:
                        entry.title = txt(kid(ev, "InstanceDescription"), "Title")
                        break
            out.append(entry)
    return out, None


def analyse(entries: list[Entry], range_start: datetime, range_end: datetime) -> list:
    """Sorts and de-duplicates the entries and inserts Gap rows; marks overlaps.

    Only entries overlapping [range_start, range_end) are kept. A gap is any period without an
    entry, including the start and end of the range.
    """
    seen, uniq = set(), []
    for e in sorted(entries, key=lambda e: (e.start, e.end)):
        key = (e.crid, e.start)
        if key not in seen and e.end > range_start and e.start < range_end:
            seen.add(key)
            uniq.append(e)
    rows, cursor = [], range_start
    for e in uniq:
        if e.start > cursor:
            rows.append(Gap(cursor, e.start))
        elif e.start < cursor and rows:
            e.status = "overlap"
            e.overlap_min = (min(cursor, e.end) - e.start).total_seconds() / 60
        rows.append(e)
        cursor = max(cursor, e.end)
    if cursor < range_end:
        rows.append(Gap(cursor, range_end))
    return rows


def _lcns(doc: ServiceListDoc) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in doc.lcn_tables:
        for lcn in t.findall("d:LCN", doc.nsmap):
            ref, num = lcn.get("serviceRef"), lcn.get("channelNumber")
            if ref and num and num.isdigit():
                out[ref] = min(out.get(ref, int(num)), int(num))
    return out


def collect_service(client: HttpClient, doc: ServiceListDoc, svc: Service, lcn: int | None,
                    windows: list[tuple[datetime, datetime]], range_start, range_end) -> ServiceEpg:
    se = ServiceEpg(svc.uid, svc.name, lcn)
    cgs = doc.cg_source_for(svc)
    se.endpoint = doc.endpoint(cgs, "ScheduleInfoEndpoint")
    if cgs is None or not se.endpoint:
        se.errors.append("no content guide source" if cgs is None else "no ScheduleInfoEndpoint")
        return se
    se.sid = doc.cg_service_id(svc)
    entries = []
    for bs, be in windows:
        got, err = fetch_entries(client, se.endpoint, se.sid, bs, be)
        se.requests += 1
        entries += got
        if err:
            se.errors.append(err)
    se.rows = analyse(entries, range_start, range_end)
    return se


def collect(location: str, cfg: Config, services: str | None = None,
            now: datetime | None = None) -> tuple[ServiceListDoc, list[ServiceEpg], datetime, datetime]:
    client = HttpClient(cfg.user_agent, cfg.timeout, cfg.retries)
    src = load(location, client, cfg.postcode)
    doc = ServiceListDoc(src.data, src.location)
    tz = ZoneInfo(cfg.cg_timezone)
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    range_start = datetime.combine(today - timedelta(days=cfg.cg_days_past), time(0), tz)
    range_end = datetime.combine(today + timedelta(days=cfg.cg_days_future + 1), time(0), tz)
    hours = cfg.cg_window_hours if cfg.cg_window_hours in (6, 12) else 12
    windows = schedule_blocks(range_start, range_end, hours)
    lcns = _lcns(doc)
    svcs = [s for s in doc.services if "end-of-service-list" not in s.uid]
    if services:
        rx = re.compile(services, re.I)
        svcs = [s for s in svcs if rx.search(s.name or "") or rx.search(s.uid)]
    if cfg.cg_max_services:
        svcs = svcs[: cfg.cg_max_services]
    svcs.sort(key=lambda s: (lcns.get(s.uid) is None, lcns.get(s.uid) or 0, (s.name or "").lower()))
    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        result = list(ex.map(lambda s: collect_service(client, doc, s, lcns.get(s.uid), windows,
                                                       range_start, range_end), svcs))
    return doc, result, range_start, range_end


# ------------------------------------------------------------------ workbook
COLUMNS = [("Date", 16), ("Start", 8), ("End", 8), ("Duration (min)", 9), ("Title", 46),
           ("Synopsis", 9), ("Image", 9), ("Status", 22), ("CRID", 44)]


def write_workbook(path: Path, doc: ServiceListDoc, result: list[ServiceEpg], range_start: datetime,
                   range_end: datetime, cfg: Config) -> Path:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.hyperlink import Hyperlink

    tz = range_start.tzinfo
    gap_min = cfg.cg_max_gap_minutes

    def fill(c):
        return PatternFill("solid", start_color=c, end_color=c)

    st = {
        "block": (Font(bold=True, color="FFFFFF", size=12), fill("1F3864")),
        "sub": (Font(italic=True, color="404040"), fill("D9E1F2")),
        "head": (Font(bold=True), fill("BFBFBF")),
        "gap": (Font(bold=True, color="9C0006"), fill("FFC7CE")),
        "small": (Font(color="7F6000"), fill("FFEB9C")),
        "overlap": (Font(bold=True, color="833C0B"), fill("F8CBAD")),
        "no": (Font(color="9C0006"), fill("FFC7CE")),
        "na": (Font(color="7F7F7F", italic=True), None),
        "err": (Font(color="9C0006"), None),
    }

    wb = Workbook(write_only=True)
    ov = wb.create_sheet("Overview")
    ws = wb.create_sheet("EPG")
    for sheet in (ov, ws):  # print: landscape, all columns on one page width
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = "9"  # A4
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0

    def cell(sheet, value, style=None, fmt=None, link=None, wrap=False):
        c = WriteOnlyCell(sheet, value=value)
        if style:
            font, f = st[style]
            c.font = font
            if f is not None:
                c.fill = f
        if fmt:
            c.number_format = fmt
        if link:  # (cell reference, internal target) – a jump within the workbook
            c.hyperlink = Hyperlink(ref=link[0], location=link[1])
            c.font = Font(color="0563C1", underline="single")
        if wrap:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        return c

    def local(dt: datetime) -> datetime:
        return dt.astimezone(tz).replace(tzinfo=None)

    # Layout first, so that the overview can link to each block's row in the EPG sheet.
    starts, row = [], 1
    for se in result:
        starts.append(row)
        row += 3 + len(se.errors[:5]) + max(len(se.rows), 1) + 1

    # ---------------- overview
    for i, w in enumerate([8, 34, 12, 14, 14, 12, 12, 14, 12, 14, 9, 60], start=1):
        ov.column_dimensions[chr(64 + i)].width = w
    ncol = 12

    def banner(sheet, text, style, width):  # styled across the full width, so the text stays readable
        return [cell(sheet, text, style)] + [cell(sheet, None, style) for _ in range(width - 1)]

    ov.append(banner(ov, f"EPG analysis · {doc.name}", "block", ncol))
    ov.append(banner(ov, f"Period {local(range_start):%Y-%m-%d} – {local(range_end) - timedelta(minutes=1):%Y-%m-%d} "
                         f"({cfg.cg_timezone}), requests of {cfg.cg_window_hours} h (A177 6.5.2.1) · "
                         f"created {datetime.now(tz):%Y-%m-%d %H:%M}", "sub", ncol))
    ov.append([cell(ov, "Legend:"), cell(ov, f"gap > {gap_min} min", "gap"),
               cell(ov, f"gap ≤ {gap_min} min", "small"), cell(ov, "overlap", "overlap"),
               cell(ov, "synopsis / image missing", "no")])
    ov.append([])
    heads = ["LCN", "Service", "Entries", "Gaps", "Gap (min)", "Small gaps", "Overlaps", "Coverage",
             "Synopsis", "Preview image", "No PI", "Remarks"]
    ov.append([cell(ov, h, "head") for h in heads])
    ov.freeze_panes = "C6"
    for n, (se, r0) in enumerate(zip(result, starts)):
        s = se.summary(range_start, range_end, gap_min)
        link = (f"B{6 + n}", f"'EPG'!A{r0}")
        remarks = "; ".join(se.errors[:2]) + (f" (+{len(se.errors) - 2} more)" if len(se.errors) > 2 else "")
        ov.append([
            cell(ov, se.lcn), cell(ov, se.name, link=link),
            cell(ov, s["entries"]),
            cell(ov, s["gaps"], "gap" if s["gaps"] else None),
            cell(ov, s["gap_minutes"], "gap" if s["gaps"] else None),
            cell(ov, s["small_gaps"], "small" if s["small_gaps"] else None),
            cell(ov, s["overlaps"], "overlap" if s["overlaps"] else None),
            cell(ov, s["coverage"], "gap" if se.rows and s["coverage"] < cfg.cg_min_coverage else None, "0.0%"),
            cell(ov, s["synopsis"], "no" if s["synopsis"] is not None and s["synopsis"] < 1 else None, "0%"),
            cell(ov, s["image"], "no" if s["image"] is not None and s["image"] < 1 else None, "0%"),
            cell(ov, s["no_pi"], "no" if s["no_pi"] else None),
            cell(ov, remarks, "err" if se.errors else None),
        ])

    # ---------------- EPG blocks
    for i, (_, w) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[chr(64 + i)].width = w
    for se in result:
        s = se.summary(range_start, range_end, gap_min)
        lcn = f"LCN {se.lcn} · " if se.lcn is not None else ""
        ws.append(banner(ws, f"{lcn}{se.name}", "block", len(COLUMNS)))
        sub = (f"{se.uid} · sid {se.sid or '–'} · {s['entries']} entries, {s['gaps']} gaps "
               f"({s['gap_minutes']} min), {s['overlaps']} overlaps · {se.endpoint or 'no endpoint'}")
        ws.append(banner(ws, sub, "sub", len(COLUMNS)))
        for err in se.errors[:5]:
            ws.append([cell(ws, "Request failed:" if se.endpoint else "Note:", "err"), None, None, None,
                       cell(ws, err, "err")])
        ws.append([cell(ws, h, "head") for h, _ in COLUMNS])
        if not se.rows:
            ws.append([cell(ws, "no entries", "na")])
        for r in se.rows:
            a, b = local(r.start), local(r.end)
            if isinstance(r, Gap):
                style = "gap" if r.minutes > gap_min else "small"
                label = "no data" if not se.entries else ("gap" if r.minutes > gap_min else "small gap")
                ws.append([cell(ws, a, style, "yyyy-mm-dd ddd"), cell(ws, a, style, "hh:mm"),
                           cell(ws, b, style, "hh:mm"), cell(ws, round(r.minutes), style),
                           cell(ws, f"— {label} —", style), cell(ws, None, style), cell(ws, None, style),
                           cell(ws, f"{label} {r.minutes:.0f} min", style), cell(ws, None, style)])
                continue
            ov_style = "overlap" if r.status == "overlap" else None

            def yn(v):
                if v is None:
                    return cell(ws, "no PI", "na")
                return cell(ws, "yes") if v else cell(ws, "no", "no")

            status = f"overlap {r.overlap_min:.0f} min" if r.status == "overlap" else "ok"
            ws.append([cell(ws, a, ov_style, "yyyy-mm-dd ddd"), cell(ws, a, ov_style, "hh:mm"),
                       cell(ws, b, None, "hh:mm"), cell(ws, round((r.end - r.start).total_seconds() / 60)),
                       cell(ws, r.title), yn(r.synopsis), yn(r.image), cell(ws, status, ov_style),
                       cell(ws, r.crid)])
        ws.append([])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def run_epg(location: str, cfg: Config, out: Path, services: str | None = None) -> tuple[Path, list[ServiceEpg]]:
    doc, result, rs, re_ = collect(location, cfg, services)
    if out.suffix.lower() != ".xlsx":
        out = out / f"dvbi-epg-{datetime.now():%Y%m%d-%H%M}.xlsx"
    return write_workbook(out, doc, result, rs, re_, cfg), result
