"""Local mock server for content guide, image, AIT and MPD endpoints (tests only)."""
from __future__ import annotations

import io
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from PIL import Image

TVA = "urn:tva:metadata:2024"
NOW = "crid://dvb.org/metadata/schedules/now-next/now"
LATER = "crid://dvb.org/metadata/schedules/now-next/later"


def png(w, h, rgba=False, color=(20, 90, 200)) -> bytes:
    im = Image.new("RGBA" if rgba else "RGB", (w, h), color + ((0,) if rgba else ()))
    if rgba:
        for x in range(w // 4, 3 * w // 4):
            for y in range(h // 4, 3 * h // 4):
                im.putpixel((x, y), color + (255,))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def jpeg(w, h) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), (200, 60, 30)).save(b, "JPEG", quality=70)
    return b.getvalue()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def program_info(crid: str, base: str, member: str | None = None, with_image: bool = True) -> str:
    mem = (f'<MemberOf xsi:type="MemberOfType" crid="{member}" index="1"/>' if member else "")
    img = (f'<RelatedMaterial><HowRelated href="urn:tva:metadata:cs:HowRelatedCS:2012:19"/>'
           f'<MediaLocator><MediaUri contentType="image/jpeg">{base}/epg/img.jpg</MediaUri></MediaLocator>'
           f'</RelatedMaterial>') if with_image else ""
    return (f'<ProgramInformation programId="{crid}"><BasicDescription>'
            f'<Title type="main">Programme {crid.rsplit("/", 1)[-1]}</Title>'
            f'<Synopsis length="medium">Programme description.</Synopsis>{img}'
            f'</BasicDescription>{mem}</ProgramInformation>')


def event(crid: str, start: datetime, minutes: int, instance_desc: bool = True) -> str:
    idesc = ('<InstanceDescription><Genre type="other" href="urn:dvb:metadata:cs:LinkedApplicationCS:2019:1.2"/>'
             '</InstanceDescription>') if instance_desc else ""
    return (f'<ScheduleEvent><Program crid="{crid}"/>{idesc}'
            f'<PublishedStartTime>{iso(start)}</PublishedStartTime>'
            f'<PublishedDuration>PT{minutes}M</PublishedDuration></ScheduleEvent>')


def tva(body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><TVAMain xmlns="{TVA}" '
            f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xml:lang="en">'
            f'<ProgramDescription>{body}</ProgramDescription></TVAMain>').encode()


AIT_OK = """<?xml version="1.0" encoding="UTF-8"?>
<mhp:ServiceDiscovery xmlns:mhp="urn:dvb:mhp:2009" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
 <mhp:ApplicationDiscovery DomainName="example.org">
  <mhp:ApplicationList>
   <mhp:Application>
    <mhp:appName Language="eng">Event app</mhp:appName>
    <mhp:applicationIdentifier><mhp:orgId>123</mhp:orgId><mhp:appId>1</mhp:appId></mhp:applicationIdentifier>
    <mhp:applicationDescriptor>
     <mhp:type><mhp:OtherApp>application/vnd.hbbtv.xhtml+xml</mhp:OtherApp></mhp:type>
     <mhp:controlCode>AUTOSTART</mhp:controlCode>
     <mhp:visibility>VISIBLE_ALL</mhp:visibility>
     <mhp:serviceBound>false</mhp:serviceBound>
     <mhp:priority>1</mhp:priority>
     <mhp:version>01</mhp:version>
     <mhp:mhpVersion><mhp:profile>0</mhp:profile><mhp:versionMajor>1</mhp:versionMajor><mhp:versionMinor>7</mhp:versionMinor><mhp:versionMicro>1</mhp:versionMicro></mhp:mhpVersion>
    </mhp:applicationDescriptor>
    <mhp:applicationTransport xsi:type="mhp:HTTPTransportType"><mhp:URLBase>{base}/app/</mhp:URLBase></mhp:applicationTransport>
    <mhp:applicationLocation>index.html</mhp:applicationLocation>
   </mhp:Application>
  </mhp:ApplicationList>
 </mhp:ApplicationDiscovery>
</mhp:ServiceDiscovery>"""

AIT_BAD = """<?xml version="1.0" encoding="UTF-8"?>
<mhp:ServiceDiscovery xmlns:mhp="urn:dvb:mhp:2009"><mhp:ApplicationDiscovery DomainName="example.org"><mhp:ApplicationList>
 <mhp:Application>
  <mhp:applicationIdentifier><mhp:orgId>abc</mhp:orgId><mhp:appId>1</mhp:appId></mhp:applicationIdentifier>
  <mhp:applicationDescriptor><mhp:type><mhp:OtherApp>text/html</mhp:OtherApp></mhp:type><mhp:controlCode>PRESENT</mhp:controlCode></mhp:applicationDescriptor>
 </mhp:Application>
</mhp:ApplicationList></mhp:ApplicationDiscovery></mhp:ServiceDiscovery>"""


def mpd(dynamic=True, dvb=True, width=1280, height=720, bw=5000000, video=True) -> bytes:
    prof = "urn:dvb:dash:profile:dvb-dash:2014,urn:mpeg:dash:profile:isoff-live:2011" if dvb else "urn:mpeg:dash:profile:isoff-live:2011"
    head = (f'type="dynamic" availabilityStartTime="2026-01-01T00:00:00Z" minimumUpdatePeriod="PT10S"'
            if dynamic else 'type="static" mediaPresentationDuration="PT1H"')
    utc = '<UTCTiming schemeIdUri="urn:mpeg:dash:utc:http-xsdate:2014" value="https://time.example.org/"/>' if dynamic else ""
    vid = (f'<AdaptationSet contentType="video" mimeType="video/mp4" segmentAlignment="true">'
           f'<Representation id="v1" codecs="avc3.640020" bandwidth="{bw}" width="{width}" height="{height}"/>'
           f'</AdaptationSet>') if video else ""
    return (f'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="{prof}" {head} '
            f'minBufferTime="PT2S"><Period id="1" start="PT0S">{vid}'
            f'<AdaptationSet contentType="audio" mimeType="audio/mp4" lang="en">'
            f'<Representation id="a1" codecs="mp4a.40.2" bandwidth="128000" audioSamplingRate="48000"/>'
            f'</AdaptationSet></Period>{utc}</MPD>').encode()


class Handler(BaseHTTPRequestHandler):
    base = ""
    last_refval_headers: dict = {}

    def log_message(self, *a):  # quiet
        pass

    def send(self, data: bytes, ct: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        """Mimics the DVB-I reference validator's /validate_sl_json?results=all endpoint."""
        u = urlsplit(self.path)
        if u.path != "/validate_sl_json":
            return self.send(b"not found", "text/plain", 404)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        Handler.last_refval_headers = dict(self.headers)
        de = (self.headers.get("X-variants") or "").lower() == "de"
        line = next(i for i, l in enumerate(body.splitlines(), 1)
                    if "<UniqueIdentifier>tag:example.org,2026:one<" in l)
        errs = {
            "fatals": [],
            "errors": [{"type": "(E)", "code": "SL999", "message": "synthetic reference validator error",
                        "element": "<UniqueIdentifier>tag:example.org,2026:one</UniqueIdentifier>", "line": line}],
            # the DE variant permits a missing NetworkID, so only the standard variant reports it
            "warnings": [] if de else [{"type": "(W)", "code": "SL998",
                                        "message": "DVBCDeliveryParameters without NetworkID", "line": 1}],
            "informationals": [],
            "errorDescriptions": [{"code": "SL999", "description": "test description", "clause": "A177 table 15"}],
        }
        return self.send(json.dumps({"errs": errs}).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == "/logo/ok.png":
            v = q.get("image_variant", "16x9_colour")
            if "w" in q:
                w = int(q["w"])
                return self.send(png(w, w * 9 // 16), "image/png")
            if v in ("16x9_colour_light", "16x9_colour_dark"):
                return self.send(png(320, 180, rgba=True, color=(250, 250, 250) if v.endswith("light") else (10, 10, 10)),
                                 "image/png")
            return self.send(png(320, 180), "image/png")
        if p == "/logo/bad.png":   # wrong width, ignores variants
            return self.send(png(200, 200), "image/png")
        if p == "/ait/ok.aitx":
            return self.send(AIT_OK.format(base=self.base).encode(), "application/vnd.dvb.ait+xml")
        if p == "/ait/bad.aitx":
            return self.send(AIT_BAD.encode(), "text/plain")
        if p == "/app/index.html":
            return self.send(b"<!DOCTYPE html><html><body>App</body></html>", "application/vnd.hbbtv.xhtml+xml")
        if p == "/mpd/live.mpd":
            return self.send(mpd(), "application/dash+xml")
        if p == "/mpd/low.mpd":
            return self.send(mpd(dynamic=False, dvb=False, width=640, height=360, bw=1000000), "application/xml")
        if p == "/mpd/radio.mpd":
            return self.send(mpd(video=False), "application/dash+xml")
        if p == "/epg/img.jpg":
            return self.send(jpeg(640, 360), "image/jpeg")
        if p == "/cg/schedule":
            return self.schedule(q)
        if p == "/cg/program":
            pid = q.get("pid", "")
            body = f'<ProgramInformationTable xml:lang="en">{program_info(pid, self.base)}</ProgramInformationTable>'
            return self.send(tva(body), "application/xml")
        self.send(b"not found", "text/plain", 404)

    def schedule(self, q):
        sid = q.get("sid", "")
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if sid.endswith("empty"):
            body = f'<ProgramLocationTable><Schedule serviceIDRef="{sid}" start="{iso(now)}" end="{iso(now)}"/></ProgramLocationTable>'
            return self.send(tva(body), "application/xml")
        if q.get("now_next") == "true":
            s_now = now - timedelta(minutes=10)
            crids = ["crid://test.de/now1", "crid://test.de/next1"]
            pis = program_info(crids[0], self.base, NOW) + program_info(crids[1], self.base, LATER)
            groups = "".join(
                f'<GroupInformation groupId="{g}" ordered="true" numOfItems="1">'
                f'<GroupType xsi:type="ProgramGroupTypeType" value="otherCollection"/><BasicDescription/></GroupInformation>'
                for g in (NOW, LATER))
            evs = event(crids[0], s_now, 30) + event(crids[1], s_now + timedelta(minutes=30), 30)
            body = (f'<ProgramInformationTable xml:lang="en">{pis}</ProgramInformationTable>'
                    f'<GroupInformationTable>{groups}</GroupInformationTable>'
                    f'<ProgramLocationTable><Schedule serviceIDRef="{sid}" start="{iso(s_now)}" '
                    f'end="{iso(s_now + timedelta(hours=1))}">{evs}</Schedule></ProgramLocationTable>')
            return self.send(tva(body), "application/xml")
        start = datetime.fromtimestamp(int(q["start"]), timezone.utc)
        end = datetime.fromtimestamp(int(q["end"]), timezone.utc)
        if sid.endswith("gap") and start < now - timedelta(days=1):
            body = f'<ProgramLocationTable><Schedule serviceIDRef="{sid}" start="{iso(start)}" end="{iso(end)}"/></ProgramLocationTable>'
            return self.send(tva(body), "application/xml")
        pis, evs, t, i = [], [], start, 0
        while t < end:
            crid = f"crid://test.de/{int(t.timestamp())}"
            pis.append(program_info(crid, self.base, with_image=not sid.endswith("gap")))
            evs.append(event(crid, t, 60, instance_desc=not sid.endswith("gap")))
            t += timedelta(hours=1)
            i += 1
        body = (f'<ProgramInformationTable xml:lang="en">{"".join(pis)}</ProgramInformationTable>'
                f'<ProgramLocationTable><Schedule serviceIDRef="{sid}" start="{iso(start)}" end="{iso(end)}">'
                f'{"".join(evs)}</Schedule></ProgramLocationTable>')
        return self.send(tva(body), "application/xml")


class MockServer:
    def __enter__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        Handler.base = self.base
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
