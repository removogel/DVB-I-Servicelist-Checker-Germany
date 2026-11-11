"""Tests for dvbi-check (run with `python -m unittest` or pytest).

Schema tests require the dvb-i-tools resources. Without network access a local
directory can be specified via DVBI_CHECK_RESOURCES.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from dvbi_check.checks.contentguide import parse_duration, parse_time, schedule_blocks  # noqa: E402
from dvbi_check.config import Config  # noqa: E402
from dvbi_check.httpclient import HttpClient, add_query  # noqa: E402
from dvbi_check.report import write_reports  # noqa: E402
from dvbi_check.resources import ResourceError, locate  # noqa: E402
from dvbi_check.runner import run  # noqa: E402
from mockserver import Handler, MockServer  # noqa: E402

FIXTURE = HERE / "fixtures" / "mini_sl.xml"


def resources_dir():
    d = os.environ.get("DVBI_CHECK_RESOURCES")
    if d:
        return d
    try:
        locate(allow_download=False)
        return None
    except ResourceError:
        return "MISSING"


def codes(rr, module):
    return [f.code for f in rr.module(module).findings]


def by_code(rr, module, code):
    return [f for f in rr.module(module).findings if f.code == code]


class TestHelpers(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(parse_duration("PT1H30M"), timedelta(hours=1, minutes=30))
        self.assertEqual(parse_duration("P1DT2S"), timedelta(days=1, seconds=2))
        self.assertIsNone(parse_duration("PT"))
        self.assertIsNone(parse_duration("1h"))

    def test_parse_time(self):
        utc = timezone.utc
        # forms used by TV-Anytime/A177; "Z" and 3-digit fractions need normalising before Python 3.11
        self.assertEqual(parse_time("2026-10-28T16:15:00Z"), datetime(2026, 10, 28, 16, 15, tzinfo=utc))
        self.assertEqual(parse_time("2013-09-25T12:30:00.000Z"), datetime(2013, 9, 25, 12, 30, tzinfo=utc))
        self.assertEqual(parse_time(" 2026-10-28T17:15:00.5+01:00 "),
                         datetime(2026, 10, 28, 16, 15, 0, 500000, tzinfo=utc))
        self.assertEqual(parse_time("2026-10-28T16:15:00"), datetime(2026, 10, 28, 16, 15, tzinfo=utc))
        self.assertIsNone(parse_time("28.10.2026 16:15"))
        self.assertIsNone(parse_time(""))

    def test_schedule_blocks(self):
        tz = ZoneInfo("Europe/Berlin")
        # range across the DST change of 2026-10-25 (25-hour day)
        first = datetime(2026, 10, 24, tzinfo=tz)
        last = datetime(2026, 10, 27, tzinfo=tz)
        for hours in (6, 12):
            blocks = schedule_blocks(first, last, hours)
            for a, b in blocks:
                self.assertEqual(int(a.timestamp()) % 10800, 0)           # A177 6.5.2.1: 3-hour boundaries
                self.assertEqual((b - a).total_seconds(), hours * 3600)   # 6 or 12 hours
            for (_, b), (a, _) in zip(blocks, blocks[1:]):
                self.assertEqual(a, b)                                    # contiguous
            self.assertLessEqual(blocks[0][0], first.astimezone(timezone.utc))
            self.assertGreaterEqual(blocks[-1][1], last.astimezone(timezone.utc))

    def test_add_query(self):
        self.assertEqual(add_query("https://a.de/x/", sid="tag:a.de,2024:x", now_next="true"),
                         "https://a.de/x/?sid=tag:a.de,2024:x&now_next=true")
        self.assertEqual(add_query("https://a.de/l.png?v=1", w=160), "https://a.de/l.png?v=1&w=160")


class TestWindowBoundaries(unittest.TestCase):
    """CG-WIN-BOUNDARY: exact detection with inclusive=true, heuristic without."""

    def _blocks(self, inclusive: bool, drop: bool):
        from dvbi_check.checks.contentguide import Block, Event
        t0 = datetime(2026, 11, 12, 21, tzinfo=timezone.utc)
        blocks, length = [], timedelta(minutes=90)
        grid = [t0 - timedelta(minutes=40) + k * length for k in range(40)]   # programmes span every boundary
        for i in range(4):
            bs, be = t0 + i * timedelta(hours=12), t0 + (i + 1) * timedelta(hours=12)
            evs = [Event(f"crid://example.org/{int(t.timestamp())}", t, length, True) for t in grid
                   if (bs <= t < be or (inclusive and t < bs < t + length)) and not (drop and t + length > be)]
            blocks.append(Block(bs, be, f"https://cg.example.org/?b={i}", True, evs))
        return blocks

    def _run(self, inclusive: bool, drop: bool):
        from dvbi_check.checks.contentguide import (ServiceCG, check_window_boundaries, inclusive_state,
                                                    merge_events)
        blocks = self._blocks(inclusive, drop)
        svc = type("S", (), {"uid": "tag:example.org,2026:x", "name": "X"})()
        sc = ServiceCG(svc)
        sc.stats["inclusive"] = inclusive_state(blocks)
        ctx = type("C", (), {"cfg": Config()})()
        check_window_boundaries(ctx, sc, merge_events(blocks), blocks, ZoneInfo("Europe/Berlin"))
        return sc

    def test_exact_with_inclusive(self):
        sc = self._run(inclusive=True, drop=True)
        self.assertEqual(sc.stats["inclusive"], "supported")
        self.assertEqual(sc.stats["window_boundary_drops"], 3)        # one programme per boundary
        self.assertEqual([f.code for f in sc.findings], ["CG-WIN-BOUNDARY"])
        self.assertFalse(self._run(inclusive=True, drop=False).findings)

    def test_heuristic_without_inclusive(self):
        sc = self._run(inclusive=False, drop=True)
        self.assertEqual(sc.stats["inclusive"], "not supported")
        self.assertEqual(sc.stats["window_boundaries_uncovered"], 3)
        self.assertEqual([f.code for f in sc.findings], ["CG-WIN-BOUNDARY"])
        self.assertFalse(self._run(inclusive=False, drop=False).findings)


class TestHttpCache(unittest.TestCase):
    def test_cache_is_bounded(self):
        with MockServer() as m:
            c = HttpClient(cache_bytes=3000, cache_item_bytes=2000)
            urls = [f"{m.base}/logo/ok.png?n={i}" for i in range(20)]
            for u in urls:
                self.assertTrue(c.get(u).ok)
            self.assertLessEqual(c._cache_size, 3000)                       # LRU keeps memory bounded
            self.assertEqual(c._cache_size, sum(len(r.content) for r in c._cache.values()))
            n = c.request_count
            c.get(urls[-1])                                                  # most recent: from cache
            self.assertEqual(c.request_count, n)
            c.get(urls[0])                                                   # evicted: fetched again
            self.assertEqual(c.request_count, n + 1)
            c.get(f"{m.base}/cg/schedule?sid=x&now_next=true", use_cache=False)
            self.assertFalse([k for k in c._cache if "schedule" in k[0]])


class TestFullRun(unittest.TestCase):
    """Full run of all modules against a local mock server."""

    @classmethod
    def setUpClass(cls):
        res = resources_dir()
        cls.has_schemas = res != "MISSING"
        cls.tmp = Path(tempfile.mkdtemp())
        with MockServer() as m:
            sl = cls.tmp / "sl.xml"
            sl.write_text(FIXTURE.read_text(encoding="utf-8").replace("{BASE}", m.base), encoding="utf-8")
            cfg = Config(cg_days_past=1, cg_days_future=2, workers=4, refval_url=m.base)
            cls.rr = run(str(sl), cfg, resources_dir=None if res in (None, "MISSING") else res,
                         allow_download=False)

    def test_no_fatal(self):
        self.assertIsNone(self.rr.fatal)
        self.assertEqual(self.rr.meta["list"]["revision"], "A177r6")

    def test_schema(self):
        if not self.has_schemas:
            self.skipTest("schemas not available")
        self.assertTrue(self.rr.module("schema").stats["valid_de_profile"])

    def test_profile_lcn_rules(self):
        c = codes(self.rr, "profile")
        self.assertIn("DE-LCN-ARD7", c)         # regional programme on LCN 7 and 44
        self.assertIn("DE-LCN-7MAP", c)         # wrong regional programme on LCN 7
        self.assertIn("DE-LCN-RTYPE", c)        # service in radio block without linear-radio
        self.assertIn("DE-POPUP-OOS", c)
        self.assertIn("DE-M-LCNRange", c)
        self.assertNotIn("DE-LCN-END", c)       # 3999 correctly assigned
        sat = by_code(self.rr, "profile", "DE-SAT")
        self.assertEqual([f.subject for f in sat], ["Two"])
        # "Two" has a ServiceInstance without DisplayName – optional for broadcasters (DTVP decision)
        self.assertNotIn("DE-M-DisplayName", c)
        m7 = by_code(self.rr, "profile", "DE-LCN-7MAP")[0]
        self.assertIn("ARD SR", m7.detail)

    def test_related_material_placement(self):
        rm = {(f.code, f.subject) for f in self.rr.module("a177").findings if f.code.startswith("RM")}
        self.assertIn(("RM01", "ServiceList"), rm)          # linked app at list level
        self.assertIn(("RM03", "Event Channel"), rm)        # out-of-service banner in instance
        self.assertIn(("RM04", "cg1"), rm)                  # service logo in content guide source
        self.assertIn(("RM05", "Radio Three"), rm)          # application contentType not defined by A177
        rm05 = [f for f in by_code(self.rr, "a177", "RM05") if f.subject == "Radio Three"]
        self.assertEqual({f.severity.name for f in rm05}, {"INFO"})   # may be signalled, clients may ignore it
        self.assertIn(("RM06", "ARD SR"), rm)               # HowRelatedCS:2019 in an r6 list
        self.assertFalse({s for c, s in rm if s in ("One", "Two")},
                         "correctly placed logos and linked apps must not be flagged")

    def test_location_aware_profile_rules(self):
        # linked app 1.2 at service level counts as broadband instance
        self.assertNotIn("Two", [f.subject for f in by_code(self.rr, "profile", "DE-BB")])
        # a misplaced out-of-service banner does not satisfy the pop-up requirement
        self.assertIn("Event Channel", [f.subject for f in by_code(self.rr, "profile", "DE-POPUP-OOS")])
        # only the service list logo is treated as an image, not list-level applications
        urls = [i["url"] for i in self.rr.module("images").stats["images"]]
        self.assertFalse([u for u in urls if u.endswith(".aitx")])

    def test_a177_relaxed_in_de_profile(self):
        for f in self.rr.module("a177").findings:
            if f.code.startswith(("SI05", "SI06-DVBC", "SI06-DVBT")):
                self.assertEqual(f.severity.name, "INFO")

    def test_content_guide(self):
        cg = self.rr.module("cg")
        stats = {s["service"]: s for s in cg.stats["per_service"]}
        one = stats["tag:example.org,2026:one"]
        self.assertEqual(one["now_next"], "ok")
        self.assertEqual(one["program_info"], "ok")
        self.assertEqual(one["avg_coverage"], 1.0)
        self.assertFalse([f for f in cg.findings if f.service == one["service"] and f.severity.name == "ERROR"],
                         "a complete service must not have CG errors")
        self.assertNotIn("CG-XSD", codes(self.rr, "cg"), "mock responses must be TVA schema compliant")
        two = [f.code for f in cg.findings if f.service == "tag:example.org,2026:two-empty"]
        self.assertIn("CG-NN-EMPTY", two)
        day = [f for f in cg.findings if f.code == "CG-DAY-EMPTY" and f.service == "tag:example.org,2026:two-empty"]
        self.assertEqual({f.severity.name for f in day}, {"ERROR", "WARNING"})  # future = error, past = warning
        radio = [f.code for f in cg.findings if f.service == "tag:example.org,2026:radio-gap"]
        self.assertIn("CG-PI-IMG", radio)
        self.assertIn("CG-EV-INSTDESC", radio)

    def test_schedule_windows(self):
        cg = self.rr.module("cg")
        stats = {s["service"]: s for s in cg.stats["per_service"]}
        one = stats["tag:example.org,2026:one"]
        # the mock answers non-conforming windows with 400, so no CG-HTTP means A177-conformant requests
        self.assertFalse([f for f in cg.findings if f.code == "CG-HTTP"])
        # programmes spanning midnight and request boundaries are merged: no gaps, no boundary warning
        self.assertFalse([f for f in cg.findings if f.service == one["service"]
                          and f.code in ("CG-DAY-GAPS", "CG-WIN-BOUNDARY", "CG-DAY-OVERLAP")])
        self.assertEqual(one["inclusive"], "supported")           # requests use inclusive=true
        self.assertEqual(one["window_boundary_drops"], 0)
        # faulty server dropping programmes that run past the end of the request: detected exactly,
        # because the next request returns them as "already running" (inclusive=true)
        radio = by_code(self.rr, "cg", "CG-WIN-BOUNDARY")
        self.assertEqual([f.service for f in radio], ["tag:example.org,2026:radio-gap"])
        self.assertIn("missing from the request in which they start", radio[0].detail)
        rs = stats["tag:example.org,2026:radio-gap"]
        self.assertEqual(rs["inclusive"], "supported")
        self.assertGreater(rs["window_boundary_drops"], 0)
        self.assertEqual(cg.stats["inclusive_supported"], 2)
        # with inclusive the merged schedule of the faulty server is complete from today on
        # (the mock deliberately returns nothing for past days of this service)
        self.assertEqual({off: cov for off, cov in rs["days"].items() if int(off) >= 0},
                         {off: 1.0 for off in rs["days"] if int(off) >= 0})
        nosrc = by_code(self.rr, "cg", "CG-NOSOURCE")
        self.assertTrue(nosrc)
        self.assertEqual({f.severity.name for f in nosrc}, {"WARNING"})   # source optional (DTVP decision)
        prof = codes(self.rr, "profile")
        self.assertNotIn("DE-M-CGSourceRef", prof)
        # services without any content guide source get no ContentGuideServiceRef finding
        nosrc_svc = {f.service for f in nosrc}
        self.assertFalse([f for f in by_code(self.rr, "profile", "DE-M-CGServiceRef") if f.service in nosrc_svc])

    def test_images(self):
        img = self.rr.module("images")
        rows = {r["url"].rsplit("/", 1)[-1]: r for r in img.stats["images"]}
        ok = rows["ok.png"]
        self.assertEqual(ok["width"], 320)
        self.assertTrue(all(v.startswith("ok") for v in ok["variants"].values()), ok["variants"])
        self.assertEqual(ok["width_param"], "supported")
        bad = [f.code for f in img.findings if f.subject == "Two"]
        for c in ("IMG-WIDTH", "IMG-CT-MISMATCH", "IMG-VARIANT-SAME"):
            self.assertIn(c, bad)

    def test_reports(self):
        paths = write_reports(self.rr, self.tmp / "out", ["html", "json", "csv"])
        self.assertEqual(len(paths), 3)
        html = next(p for p in paths if p.suffix == ".html").read_text(encoding="utf-8")
        self.assertIn("Test list", html)
        self.assertIn("DE-LCN-7MAP", html)


class TestAitMpd(TestFullRun):
    """Uses the same full run; checks the ait and mpd modules."""

    def test_ait(self):
        ait = self.rr.module("ait")
        self.assertEqual(ait.stats["aits_checked"], 2)
        ok = [f.code for f in ait.findings if f.subject == "Event Channel" and f.severity.name == "ERROR"]
        self.assertEqual(ok, [], "a valid AIT must not have errors")
        bad = {f.code for f in ait.findings if f.subject == "End of List"}
        for c in ("AIT-CT", "AIT-ID", "AIT-TYPE", "AIT-TRANSPORT", "AIT-LOC", "AIT-AUTOSTART", "AIT-NO-HBBTV"):
            self.assertIn(c, bad)
        # HbbTV plus an alternative HTML5 instance, both AUTOSTART: permitted by A177 5.2.4.1
        good = {f.code for f in ait.findings if f.subject == "Event Channel"}
        self.assertFalse(good & {"AIT-TYPE", "AIT-AUTOSTART", "AIT-NO-HBBTV"}, good)
        entry = ait.stats["aits"][0]["apps"] + ait.stats["aits"][1]["apps"]
        self.assertTrue(any(a.get("entry_status") == 200 for a in entry))

    def test_mpd(self):
        mpd = self.rr.module("mpd")
        by_svc = lambda name: {f.code for f in mpd.findings if f.subject == name}  # noqa: E731
        self.assertFalse(by_svc("One") - {"MPD-HTTPS"}, "valid live MPD")
        for c in ("MPD-PROFILE", "MPD-STATIC", "MPD-QUALITY", "MPD-BITRATE", "MPD-CT"):
            self.assertIn(c, by_svc("ARD Nord"))
        self.assertIn("MPD-HTTP", by_svc("ARD SWR"))
        self.assertIn("MPD-NOVIDEO", by_svc("Radio Three"))
        self.assertEqual(mpd.stats["mpds_checked"], 4)


class TestRefVal(TestFullRun):
    """Reference validator integration via its REST interface (mocked)."""

    def test_refval(self):
        rv = self.rr.module("refval")
        self.assertEqual(rv.stats["http_status"], 200)
        err = by_code(self.rr, "refval", "RV-SL999")[0]
        self.assertEqual(err.severity.name, "ERROR")
        self.assertEqual(err.service, "tag:example.org,2026:one")   # line number mapped back to the service
        self.assertEqual(err.ref, "A177 table 15")
        # DE variant requested via header; the (mocked) validator then omits the NetworkID warning
        self.assertEqual(rv.stats["variant"], "DE")
        self.assertEqual(by_code(self.rr, "refval", "RV-SL998"), [])
        self.assertEqual(Handler.last_refval_headers.get("X-variants"), "DE")


class TestEpg(unittest.TestCase):
    """EPG gap analysis (dvbi-check epg) – analysis and Excel output."""

    def test_analyse(self):
        from dvbi_check.epg import Entry, Gap, analyse
        tz = ZoneInfo("Europe/Berlin")
        d0 = datetime(2026, 11, 11, tzinfo=tz)

        def e(h, m, dur, crid):
            s = d0 + timedelta(hours=h, minutes=m)
            return Entry(s, s + timedelta(minutes=dur), crid)

        rows = analyse([e(-1, 30, 120, "a"), e(0, 30, 90, "b"), e(2, 0, 60, "c"), e(2, 0, 60, "c"),
                        e(3, 2, 58, "d"), e(5, 0, 60, "e"), e(30, 0, 60, "outside")],
                       d0, d0 + timedelta(hours=8))
        kinds = [(type(r).__name__, getattr(r, "crid", None)) for r in rows]
        self.assertEqual(kinds, [("Entry", "a"), ("Entry", "b"), ("Entry", "c"), ("Gap", None), ("Entry", "d"),
                                 ("Gap", None), ("Entry", "e"), ("Gap", None)])   # duplicate c and outside dropped
        self.assertEqual(rows[1].status, "overlap")                              # b starts before a ends
        self.assertEqual(rows[1].overlap_min, 60)
        gaps = [r.minutes for r in rows if isinstance(r, Gap)]
        self.assertEqual(gaps, [2, 60, 120])                                     # small gap, gap, trailing gap

    def test_workbook(self):
        from openpyxl import load_workbook
        from dvbi_check.cli import main
        tmp = Path(tempfile.mkdtemp())
        with MockServer() as m:
            sl = tmp / "sl.xml"
            sl.write_text(FIXTURE.read_text(encoding="utf-8").replace("{BASE}", m.base), encoding="utf-8")
            rc = main(["epg", str(sl), "--out", str(tmp / "epg.xlsx"), "--cg-days-past", "1",
                       "--cg-days-future", "2", "--workers", "4"])
        self.assertEqual(rc, 0)
        wb = load_workbook(tmp / "epg.xlsx")
        self.assertEqual(wb.sheetnames, ["Overview", "EPG"])
        ov, ws = wb["Overview"], wb["EPG"]
        head = [c.value for c in ov[5]]
        rows = {r[1]: dict(zip(head, r)) for r in ov.iter_rows(min_row=6, values_only=True) if r[1]}
        self.assertEqual(rows["One"]["Gaps"], 0)                 # A177-conformant server: no gaps
        self.assertEqual(rows["One"]["Synopsis"], 1)
        self.assertGreater(rows["Radio Three"]["Gaps"], 0)       # server drops programmes at request ends
        self.assertEqual(rows["Radio Three"]["Preview image"], 0)
        # server that drops programmes at the end of a request (detected via inclusive=true)
        self.assertIn("missing from the request in which they start", rows["Radio Three"]["Remarks"])
        self.assertIsNone(rows["One"]["Remarks"])
        self.assertIn("no content guide source", rows["ARD Nord"]["Remarks"])
        # every overview link jumps to the header of its block
        for r in range(6, 6 + len(rows)):
            c = ov.cell(r, 2)
            target = ws[c.hyperlink.location.split("!")[1]].value
            self.assertTrue(target.endswith(c.value), (c.value, target))
        self.assertTrue([r for r in ws.iter_rows(values_only=True) if r and r[0] == "Server:"])
        gap_rows = [r for r in ws.iter_rows(values_only=True) if r[4] == "— gap —"]
        self.assertTrue(gap_rows)
        self.assertTrue(all(r[0] and r[1] and r[2] for r in gap_rows))


class TestOffline(unittest.TestCase):
    def test_offline_skips_network_modules(self):
        res = resources_dir()
        rr = run(str(FIXTURE), Config(), offline=True,
                 resources_dir=None if res in (None, "MISSING") else res, allow_download=False)
        self.assertEqual(rr.module("cg").status, "skipped")
        for m in ("images", "ait", "mpd", "refval"):
            self.assertEqual(rr.module(m).status, "skipped")
        self.assertNotEqual(rr.module("profile").status, "skipped")


if __name__ == "__main__":
    unittest.main()
