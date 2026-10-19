"""Lokaler Mock-Server für Content-Guide- und Image-Endpunkte (nur für Tests)."""
from __future__ import annotations

import io
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
            f'<Title type="main">Sendung {crid.rsplit("/", 1)[-1]}</Title>'
            f'<Synopsis length="medium">Beschreibung der Sendung.</Synopsis>{img}'
            f'</BasicDescription>{mem}</ProgramInformation>')


def event(crid: str, start: datetime, minutes: int, instance_desc: bool = True) -> str:
    idesc = ('<InstanceDescription><Genre type="other" href="urn:dvb:metadata:cs:LinkedApplicationCS:2019:1.2"/>'
             '</InstanceDescription>') if instance_desc else ""
    return (f'<ScheduleEvent><Program crid="{crid}"/>{idesc}'
            f'<PublishedStartTime>{iso(start)}</PublishedStartTime>'
            f'<PublishedDuration>PT{minutes}M</PublishedDuration></ScheduleEvent>')


def tva(body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><TVAMain xmlns="{TVA}" '
            f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xml:lang="de">'
            f'<ProgramDescription>{body}</ProgramDescription></TVAMain>').encode()


class Handler(BaseHTTPRequestHandler):
    base = ""

    def log_message(self, *a):  # still
        pass

    def send(self, data: bytes, ct: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
        if p == "/logo/bad.png":   # falsche Breite, ignoriert Varianten
            return self.send(png(200, 200), "image/png")
        if p == "/epg/img.jpg":
            return self.send(jpeg(640, 360), "image/jpeg")
        if p == "/cg/schedule":
            return self.schedule(q)
        if p == "/cg/program":
            pid = q.get("pid", "")
            body = f'<ProgramInformationTable xml:lang="de">{program_info(pid, self.base)}</ProgramInformationTable>'
            return self.send(tva(body), "application/xml")
        self.send(b"not found", "text/plain", 404)

    def schedule(self, q):
        sid = q.get("sid", "")
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if sid.endswith("leer"):
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
            body = (f'<ProgramInformationTable xml:lang="de">{pis}</ProgramInformationTable>'
                    f'<GroupInformationTable>{groups}</GroupInformationTable>'
                    f'<ProgramLocationTable><Schedule serviceIDRef="{sid}" start="{iso(s_now)}" '
                    f'end="{iso(s_now + timedelta(hours=1))}">{evs}</Schedule></ProgramLocationTable>')
            return self.send(tva(body), "application/xml")
        start = datetime.fromtimestamp(int(q["start"]), timezone.utc)
        end = datetime.fromtimestamp(int(q["end"]), timezone.utc)
        if sid.endswith("luecke") and start < now - timedelta(days=1):
            body = f'<ProgramLocationTable><Schedule serviceIDRef="{sid}" start="{iso(start)}" end="{iso(end)}"/></ProgramLocationTable>'
            return self.send(tva(body), "application/xml")
        pis, evs, t, i = [], [], start, 0
        while t < end:
            crid = f"crid://test.de/{int(t.timestamp())}"
            pis.append(program_info(crid, self.base, with_image=not sid.endswith("luecke")))
            evs.append(event(crid, t, 60, instance_desc=not sid.endswith("luecke")))
            t += timedelta(hours=1)
            i += 1
        body = (f'<ProgramInformationTable xml:lang="de">{"".join(pis)}</ProgramInformationTable>'
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
