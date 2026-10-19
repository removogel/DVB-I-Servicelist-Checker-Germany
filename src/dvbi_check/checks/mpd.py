"""Prüfung der Stream-Manifeste (DASH-MPDs, optional HLS) der ServiceInstances.

Grundlagen: A177r6 DASHDeliveryParameters, DVB-DASH (ETSI TS 103 285),
DTVP IT-3 (DVB-DASH 2014 Profil, Codecs) und DVB-I Book Germany 7.3
(Mindestqualität 960x540p, > 2,5 Mbit/s).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from lxml import etree

from ..model import Findings, ModuleResult, Severity, Timer
from ..sl import local
from . import Context

MPD_NS = "urn:mpeg:dash:schema:mpd:2011"
DASH_CT = "application/dash+xml"
DVB_PROFILES = ("urn:dvb:dash:profile:dvb-dash:2014", "urn:dvb:dash:profile:dvb-dash:2017")
REF_DVB = "DVB-DASH (ETSI TS 103 285)"
VIDEO_CODECS = {"avc1": "AVC", "avc3": "AVC", "hvc1": "HEVC", "hev1": "HEVC", "av01": "AV1", "vp09": "VP9", "vvc1": "VVC"}
AUDIO_CODECS = {"mp4a": "AAC", "ec-3": "E-AC-3", "ac-3": "AC-3", "ac-4": "AC-4", "mhm1": "MPEG-H", "opus": "Opus"}


@dataclass
class MpdReport:
    url: str
    kind: str                       # dash | hls
    issues: list = field(default_factory=list)
    status: int | None = None
    content_type: str = ""
    elapsed: float = 0.0
    bytes: int = 0
    info: dict = field(default_factory=dict)

    def add(self, code, sev, msg, ref="", detail=""):
        self.issues.append((code, sev, msg, ref, detail))

    def to_dict(self):
        return {"url": self.url, "kind": self.kind, "status": self.status, "content_type": self.content_type,
                "elapsed_s": round(self.elapsed, 2), "bytes": self.bytes, **self.info, "issues": len(self.issues)}


def _all(el, name):
    return el.iter(f"{{{MPD_NS}}}{name}")


def _inherit(rep_el, attr):
    """Attribut von Representation, sonst AdaptationSet."""
    v = rep_el.get(attr)
    if v is None and rep_el.getparent() is not None:
        v = rep_el.getparent().get(attr)
    return v


def check_dash(ctx: Context, url: str, is_tv: bool) -> MpdReport:
    rep = MpdReport(url, "dash")
    cfg = ctx.cfg
    if url.startswith("http://"):
        rep.add("MPD-HTTPS", Severity.WARNING, "MPD-URL ohne HTTPS")
    r = ctx.client.get(url, use_cache=True)
    rep.status, rep.elapsed, rep.content_type, rep.bytes = r.status, r.elapsed, r.content_type, len(r.content)
    if not r.ok:
        rep.add("MPD-HTTP", Severity.ERROR, "MPD nicht abrufbar", detail=r.describe())
        return rep
    if r.elapsed > cfg.mpd_slow_seconds:
        rep.add("MPD-SLOW", Severity.WARNING, f"MPD-Antwort langsamer als {cfg.mpd_slow_seconds} s",
                detail=f"{r.elapsed:.2f} s")
    if rep.content_type != DASH_CT:
        rep.add("MPD-CT", Severity.WARNING, f"Content-Type „{rep.content_type or '(leer)'}“ statt {DASH_CT}",
                ref=REF_DVB)
    try:
        root = etree.fromstring(r.content, etree.XMLParser(no_network=True, resolve_entities=False, huge_tree=True))
    except etree.XMLSyntaxError as e:
        rep.add("MPD-XML", Severity.ERROR, "MPD ist kein wohlgeformtes XML", detail=str(e))
        return rep
    q = etree.QName(root)
    if q.localname != "MPD" or q.namespace != MPD_NS:
        rep.add("MPD-ROOT", Severity.ERROR, f"Root-Element ist nicht MPD im Namespace {MPD_NS}", detail=root.tag)
        return rep
    mtype = root.get("type", "static")
    profiles = [p.strip() for p in (root.get("profiles") or "").split(",") if p.strip()]
    rep.info.update({"type": mtype, "profiles": profiles})
    if not any(p in DVB_PROFILES for p in profiles):
        rep.add("MPD-PROFILE", Severity.WARNING, "MPD signalisiert kein DVB-DASH-Profil",
                ref="DTVP IT-3 (DVB-DASH 2014) / " + REF_DVB, detail=", ".join(profiles) or "(keine @profiles)")
    if mtype != "dynamic":
        rep.add("MPD-STATIC", Severity.WARNING, "MPD ist nicht @type=\"dynamic\" (für lineare Services erwartet)",
                ref="A177 (lineare Services)", detail=f"type={mtype}")
    else:
        if not root.get("availabilityStartTime"):
            rep.add("MPD-AST", Severity.ERROR, "Dynamisches MPD ohne @availabilityStartTime", ref="ISO/IEC 23009-1")
        if root.find(f"{{{MPD_NS}}}UTCTiming") is None:
            rep.add("MPD-UTC", Severity.WARNING, "Dynamisches MPD ohne UTCTiming-Element", ref=REF_DVB)
        if not root.get("minimumUpdatePeriod") and not root.get("timeShiftBufferDepth"):
            rep.add("MPD-MUP", Severity.INFO, "Weder @minimumUpdatePeriod noch @timeShiftBufferDepth gesetzt")
    periods = list(root.iterfind(f"{{{MPD_NS}}}Period"))
    if not periods:
        rep.add("MPD-PERIOD", Severity.ERROR, "MPD ohne Period")
        return rep
    rep.info["periods"] = len(periods)

    video, audio, drm, codecs, subs = [], [], set(), Counter(), 0
    for rp in _all(root, "Representation"):
        mime = _inherit(rp, "mimeType") or ""
        ctype = rp.getparent().get("contentType", "")
        cod = _inherit(rp, "codecs") or ""
        fam = cod.split(".")[0].lower()
        if fam:
            codecs[VIDEO_CODECS.get(fam) or AUDIO_CODECS.get(fam) or fam] += 1
        bw = int(rp.get("bandwidth") or 0)
        if mime.startswith("video") or ctype == "video" or fam in VIDEO_CODECS:
            w = int(_inherit(rp, "width") or 0)
            h = int(_inherit(rp, "height") or 0)
            video.append((w, h, bw, cod))
        elif mime.startswith("audio") or ctype == "audio" or fam in AUDIO_CODECS:
            audio.append((bw, cod))
        elif "ttml" in mime or "stpp" in fam or ctype == "text":
            subs += 1
    for cp in _all(root, "ContentProtection"):
        scheme = (cp.get("schemeIdUri") or "").lower()
        if scheme.startswith("urn:uuid:"):
            drm.add(scheme[9:])
    rep.info.update({
        "video_max": max(((w, h) for w, h, _, _ in video), default=None),
        "video_max_bandwidth": max((bw for *_, bw, _ in video), default=None),
        "video_representations": len(video), "audio_representations": len(audio),
        "subtitle_representations": subs, "codecs": dict(codecs), "drm_systems": sorted(drm),
    })
    unknown = [c for c in codecs if c not in VIDEO_CODECS.values() and c not in AUDIO_CODECS.values()
               and c not in ("stpp", "wvtt")]
    if unknown:
        rep.add("MPD-CODEC", Severity.INFO, "Codec außerhalb der DTVP-Anforderungen", ref="DTVP IT-3",
                detail=", ".join(unknown))
    for name in codecs:
        if name in ("AV1", "VP9", "VVC", "AC-4", "MPEG-H", "Opus"):
            rep.add("MPD-CODEC-OPT", Severity.WARNING, f"{name} wird von DTVP-Receivern nicht verpflichtend unterstützt",
                    ref="DTVP IT-3", detail="nur zulässig, wenn zusätzlich AVC/HEVC bzw. AAC/E-AC-3 angeboten wird")
    if drm:
        rep.add("MPD-DRM", Severity.INFO, "MPD enthält ContentProtection (DRM)",
                ref="Book 11.3 (Wiedergabe über Linked App 1.2)", detail=", ".join(sorted(drm)))
    if is_tv:
        if not video:
            rep.add("MPD-NOVIDEO", Severity.ERROR, "TV-Service, aber MPD enthält keine Video-Representation")
        else:
            w, h = rep.info["video_max"]
            if w < cfg.mpd_min_width or h < cfg.mpd_min_height:
                rep.add("MPD-QUALITY", Severity.WARNING,
                        f"Höchste Videoauflösung unter {cfg.mpd_min_width}×{cfg.mpd_min_height}",
                        ref="Book 7.3 (Mindestqualität Broadband)", detail=f"{w}×{h}")
            if rep.info["video_max_bandwidth"] and rep.info["video_max_bandwidth"] < cfg.mpd_min_bandwidth:
                rep.add("MPD-BITRATE", Severity.WARNING,
                        f"Höchste Video-Bitrate unter {cfg.mpd_min_bandwidth / 1e6:.1f} Mbit/s".replace(".", ","),
                        ref="Book 7.3 (Mindestqualität Broadband)",
                        detail=f"{rep.info['video_max_bandwidth'] / 1e6:.2f} Mbit/s".replace(".", ","))
    if not audio:
        rep.add("MPD-NOAUDIO", Severity.WARNING, "MPD enthält keine Audio-Representation")
    return rep


def check_hls(ctx: Context, url: str) -> MpdReport:
    rep = MpdReport(url, "hls")
    r = ctx.client.get(url)
    rep.status, rep.elapsed, rep.content_type, rep.bytes = r.status, r.elapsed, r.content_type, len(r.content)
    if not r.ok:
        rep.add("HLS-HTTP", Severity.ERROR, "HLS-Playlist nicht abrufbar", detail=r.describe())
    elif not r.content.lstrip().startswith(b"#EXTM3U"):
        rep.add("HLS-FORMAT", Severity.ERROR, "Antwort ist keine M3U8-Playlist (#EXTM3U fehlt)")
    return rep


def collect(ctx: Context):
    doc, ns = ctx.doc, ctx.doc.nsmap
    dash: dict[str, list] = defaultdict(list)
    hls: dict[str, list] = defaultdict(list)
    for s in doc.services:
        for inst in s.el.findall("d:ServiceInstance", ns):
            for ul in inst.iterfind("d:DASHDeliveryParameters/d:UriBasedLocation", ns):
                u = doc.text(ul, "ty:URI")
                if u and s not in dash[u]:
                    dash[u].append(s)
            for od in inst.findall("d:OtherDeliveryParameters", ns):
                for u in od.iter():
                    if isinstance(u.tag, str) and local(u) == "URI" and u.text and s not in hls[u.text.strip()]:
                        hls[u.text.strip()].append(s)
    return dash, hls


def run(ctx: Context) -> ModuleResult:
    res = ModuleResult("mpd")
    f = Findings("mpd")
    with Timer() as t:
        if ctx.offline:
            res.skipped = "Offline-Modus"
        else:
            dash, hls = collect(ctx)
            with ThreadPoolExecutor(max_workers=ctx.cfg.workers) as ex:
                fd = {u: ex.submit(check_dash, ctx, u, any(s.service_type != "linear-radio" for s in svcs))
                      for u, svcs in dash.items()}
                fh = {u: ex.submit(check_hls, ctx, u) for u in hls} if ctx.cfg.check_hls else {}
            reports = []
            for group, futs in ((dash, fd), (hls, fh)):
                for u, fut in futs.items():
                    rep = fut.result()
                    reports.append(rep)
                    for code, sev, msg, ref, detail in rep.issues:
                        for s in group[u]:
                            f.add(code, sev, msg, ref=ref, service=s.uid, subject=s.name,
                                  detail=(detail + " · " if detail else "") + u)
            dr = [r for r in reports if r.kind == "dash"]
            ok = [r for r in dr if r.status and 200 <= r.status < 300]
            res.stats = {
                "mpds_checked": len(dr),
                "mpds_reachable": len(ok),
                "mpds_without_errors": sum(1 for r in dr if not any(i[1] == Severity.ERROR for i in r.issues)),
                "hls_checked": len(reports) - len(dr),
                "codecs": dict(sum((Counter(r.info.get("codecs", {})) for r in ok), Counter())),
                "avg_response_s": round(sum(r.elapsed for r in ok) / len(ok), 2) if ok else None,
                "manifests": [r.to_dict() for r in reports],
            }
    res.findings = f
    res.duration = t.elapsed
    return res
