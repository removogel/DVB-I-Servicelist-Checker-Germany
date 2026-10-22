"""Tests for dvbi-check (run with `python -m unittest` or pytest).

Schema tests require the dvb-i-tools resources. Without network access a local
directory can be specified via DVBI_CHECK_RESOURCES.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from dvbi_check.checks.contentguide import parse_duration  # noqa: E402
from dvbi_check.config import Config  # noqa: E402
from dvbi_check.httpclient import add_query  # noqa: E402
from dvbi_check.report import write_reports  # noqa: E402
from dvbi_check.resources import ResourceError, locate  # noqa: E402
from dvbi_check.runner import run  # noqa: E402
from mockserver import MockServer  # noqa: E402

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

    def test_add_query(self):
        self.assertEqual(add_query("https://a.de/x/", sid="tag:a.de,2024:x", now_next="true"),
                         "https://a.de/x/?sid=tag:a.de,2024:x&now_next=true")
        self.assertEqual(add_query("https://a.de/l.png?v=1", w=160), "https://a.de/l.png?v=1&w=160")


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
        dn = by_code(self.rr, "profile", "DE-M-DisplayName")
        self.assertEqual([f.subject for f in dn], ["Two"])
        m7 = by_code(self.rr, "profile", "DE-LCN-7MAP")[0]
        self.assertIn("ARD SR", m7.detail)

    def test_related_material_placement(self):
        rm = {(f.code, f.subject) for f in self.rr.module("a177").findings if f.code.startswith("RM")}
        self.assertIn(("RM01", "ServiceList"), rm)          # linked app at list level
        self.assertIn(("RM03", "Event Channel"), rm)        # out-of-service banner in instance
        self.assertIn(("RM04", "cg1"), rm)                  # service logo in content guide source
        self.assertIn(("RM05", "Radio Three"), rm)          # invalid application contentType
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
        self.assertIn("CG-NOSOURCE", codes(self.rr, "cg"))

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
        for c in ("AIT-CT", "AIT-ID", "AIT-TYPE", "AIT-TRANSPORT", "AIT-LOC", "AIT-AUTOSTART"):
            self.assertIn(c, bad)
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
        warn = by_code(self.rr, "refval", "RV-SL998")[0]
        self.assertEqual(warn.severity.name, "INFO")                 # relaxed by the DE profile
        self.assertIn("variants=DE", rv.stats["endpoint"])


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
