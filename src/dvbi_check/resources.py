"""Provisioning of XSD schemas and classification schemes.

Source is the BSD-licensed dvb-i-tools by Paul Higgs
(https://github.com/paulhiggs/dvb-i-tools), pinned to a commit so that
check results stay reproducible.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import requests
from lxml import etree

REPO = "paulhiggs/dvb-i-tools"
PINNED_REF = "08b16ccd8d364e298c73c94964b17243e9d6fc91"  # as of 2026-10-18, A177r6–r8

# service list namespace year -> (standard XSD, DE profile XSD or None)
SL_SCHEMAS = {
    "2019": ("dvbi_v1.0.xsd", None),
    "2020": ("dvbi_v2.0.xsd", None),
    "2021": ("dvbi_v3.0.xsd", None),
    "2022": ("dvbi_v3.1.xsd", None),
    "2022b": ("dvbi_v4.0+hls+hbbtv.xsd", None),
    "2023": ("dvbi_v5.0+hls+hbbtv.xsd", None),
    "2024": ("dvbi_v6.0+hls+hbbtv.xsd", "dvbi_v6.0+hls+hbbtv-(DE).xsd"),
    "2025": ("dvbi_v7.0+hls+hbbtv.xsd", None),
    "2026": ("dvbi_v8.0+hls+hbbtv+dvbhb.xsd", None),
}
TVA_SCHEMAS = {
    "2019": "tva_metadata_3-1_2019.xsd",
    "2023": "tva_metadata_3-1_2023.xsd",
    "2024": "tva_metadata_3-1_2024.xsd",
    "2026": "tva_metadata_3-1_v1141.xsd",
}


def cache_root() -> Path:
    return Path(os.environ.get("DVBI_CHECK_HOME", Path.home() / ".cache" / "dvbi-check"))


@dataclass(frozen=True)
class Resources:
    root: Path
    ref: str

    @property
    def schemas(self) -> Path:
        return self.root / "schemas"

    @property
    def cs(self) -> Path:
        return self.root / "res"

    def sl_schema_files(self, ns_year: str) -> tuple[Path | None, Path | None]:
        std, de = SL_SCHEMAS.get(ns_year, (None, None))
        return (self.schemas / std if std else None, self.schemas / de if de else None)

    def tva_schema_file(self, ns_year: str) -> Path | None:
        f = TVA_SCHEMAS.get(ns_year)
        return self.schemas / f if f else None


class ResourceError(RuntimeError):
    pass


def _valid(root: Path) -> bool:
    return (root / "schemas" / "dvbi_v6.0.xsd").is_file() and (root / "res").is_dir()


def locate(resources_dir: str | Path | None = None, ref: str = PINNED_REF,
           allow_download: bool = True) -> Resources:
    if resources_dir:
        root = Path(resources_dir).expanduser()
        if not _valid(root):
            raise ResourceError(f"{root} does not contain dvb-i-tools schemas (schemas/, res/)")
        return Resources(root, "local")
    root = cache_root() / f"dvb-i-tools-{ref[:12]}"
    if _valid(root):
        return Resources(root, ref)
    if not allow_download:
        raise ResourceError(f"Schemas missing in {root}. Please run 'dvbi-check resources update'.")
    download(root, ref)
    return Resources(root, ref)


def download(target: Path, ref: str = PINNED_REF) -> Path:
    """Downloads schemas/ and res/ from the GitHub archive; fallback: git."""
    target = Path(target)
    tmp = target.with_suffix(".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        try:
            _download_tarball(tmp, ref)
        except Exception as e_tar:  # noqa: BLE001
            try:
                _download_git(tmp, ref)
            except Exception as e_git:  # noqa: BLE001
                raise ResourceError(f"Download failed (archive: {e_tar}; git: {e_git})") from e_git
        if not _valid(tmp):
            raise ResourceError("Downloaded archive does not contain schemas")
        shutil.rmtree(target, ignore_errors=True)
        tmp.rename(target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return target


def _download_tarball(dest: Path, ref: str) -> None:
    url = f"https://codeload.github.com/{REPO}/tar.gz/{ref}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        for m in tf.getmembers():
            parts = Path(m.name).parts
            if len(parts) < 2 or parts[1] not in ("schemas", "res", "LICENSE.txt"):
                continue
            rel = Path(*parts[1:])
            out = (dest / rel).resolve()
            if not str(out).startswith(str(dest.resolve())):
                continue  # Path traversal
            if m.isdir():
                out.mkdir(parents=True, exist_ok=True)
            elif m.isfile():
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(tf.extractfile(m).read())


def _download_git(dest: Path, ref: str) -> None:
    repo = dest / "_git"
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    repo.mkdir()
    run("init", "-q")
    run("remote", "add", "origin", f"https://github.com/{REPO}.git")
    run("fetch", "-q", "--depth", "1", "origin", ref)
    run("checkout", "-q", "FETCH_HEAD")
    for d in ("schemas", "res"):
        shutil.copytree(repo / d, dest / d)
    shutil.copy2(repo / "LICENSE.txt", dest / "LICENSE.txt")
    shutil.rmtree(repo)


@lru_cache(maxsize=16)
def load_schema(path: str) -> etree.XMLSchema:
    parser = etree.XMLParser(resolve_entities=True, no_network=True, load_dtd=True)
    return etree.XMLSchema(etree.parse(path, parser))


@lru_cache(maxsize=1)
def load_classification_schemes(cs_dir: str) -> dict[str, set[str]]:
    terms: dict[str, set[str]] = {}
    for f in Path(cs_dir).rglob("*.xml"):
        try:
            cs = etree.parse(str(f), etree.XMLParser(no_network=True, resolve_entities=False)).getroot()
        except etree.XMLSyntaxError:
            continue
        uri = cs.get("uri")
        if not uri:
            continue
        s = terms.setdefault(uri, set())
        for t in cs.iter():
            if isinstance(t.tag, str) and t.tag.endswith("Term") and t.get("termID"):
                s.add(t.get("termID"))
    # schemes not available as files (A177 Annex C, HowRelatedCS)
    for year in ("2019", "2020", "2021"):
        terms.setdefault(f"urn:dvb:metadata:cs:HowRelatedCS:{year}", set()).update(
            {"1000.1", "1000.2", "1001.1", "1001.2", "1001.3", "1002.1"})
    return terms
