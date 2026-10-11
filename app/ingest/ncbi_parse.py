"""Parse NCBI responses into plain records, and check them before anything is loaded.

Pure functions: no network, no database. Every value stays as NCBI wrote it. Mapping to ontology
terms (UBERON, …) is the normalization step's job, not this module's.
"""
from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

# INSDC missing-value vocabulary: not a value, so no assertion (the raw file keeps it).
INSDC_NULLS = {"missing", "not applicable", "not collected", "not provided", "restricted access",
               "na", "n/a", "none", "unknown", "-", ""}


@dataclass(frozen=True)
class BioProject:
    accession: str
    uid: str
    title: str | None
    description: str | None


@dataclass(frozen=True)
class Attribute:
    attribute_name: str          # as the submitter wrote it, e.g. "Pen"
    harmonized_name: str | None  # NCBI's harmonized name, e.g. "host_taxid"; None for free attributes
    display_name: str | None
    value: str

    @property
    def field(self) -> str:
        """Field name in prov.assertion: harmonized if NCBI has one, else the normalized submitter name."""
        return self.harmonized_name or normalize_name(self.attribute_name)

    def matches(self, name: str) -> bool:
        n = normalize_name(name)
        return n in {normalize_name(self.attribute_name), normalize_name(self.harmonized_name or "")}


@dataclass(frozen=True)
class BioSample:
    accession: str               # SAMN…
    sra_accession: str | None    # SRS…
    sample_name: str | None
    title: str | None
    attributes: tuple[Attribute, ...]

    def get(self, name: str) -> str | None:
        hits = [a.value for a in self.attributes if a.matches(name)]
        return hits[0] if hits else None


@dataclass(frozen=True)
class SraRun:
    run: str
    experiment: str
    biosample: str
    library_name: str | None
    library_strategy: str | None
    library_selection: str | None
    library_source: str | None
    library_layout: str | None
    platform: str | None
    instrument_model: str | None
    spots: int | None
    bases: int | None
    avg_read_len: int | None
    release_date: date | None


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def is_null(value: str | None) -> bool:
    return value is None or value.strip().lower() in INSDC_NULLS


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    t = " ".join(el.text.split())
    return t or None


def parse_bioproject(xml: bytes, uid: str) -> BioProject:
    root = ET.fromstring(xml)
    arch = root.find(".//Project/ProjectID/ArchiveID")
    if arch is None or not arch.get("accession"):
        raise ValueError("BioProject XML has no Project/ProjectID/ArchiveID accession")
    descr = root.find(".//Project/ProjectDescr")
    return BioProject(
        accession=arch.get("accession", ""),
        uid=uid,
        title=_text(descr.find("Title")) if descr is not None else None,
        description=_text(descr.find("Description")) if descr is not None else None,
    )


def parse_biosamples(xml: bytes) -> list[BioSample]:
    out = []
    for bs in ET.fromstring(xml).findall("BioSample"):
        ids = {(i.get("db") or "", i.get("db_label") or ""): (i.text or "").strip() for i in bs.findall("./Ids/Id")}
        sra = next((v for (db, _), v in ids.items() if db == "SRA"), None)
        name = next((v for (_, label), v in ids.items() if label == "Sample name"), None)
        attrs = tuple(
            Attribute(attribute_name=(a.get("attribute_name") or "").strip(),
                      harmonized_name=(a.get("harmonized_name") or None),
                      display_name=(a.get("display_name") or None),
                      value=" ".join((a.text or "").split()))
            for a in bs.findall("./Attributes/Attribute")
        )
        out.append(BioSample(accession=bs.get("accession", ""), sra_accession=sra or None,
                             sample_name=name, title=_text(bs.find("./Description/Title")),
                             attributes=attrs))
    return out


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def _date(v: str | None) -> date | None:
    try:
        return date.fromisoformat(v[:10]) if v else None
    except ValueError:
        return None


def parse_runinfo(csv_bytes: bytes) -> list[SraRun]:
    if not csv_bytes.strip():
        return []
    rows = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    runs = []
    for r in rows:
        if not r.get("Run", "").startswith(("SRR", "ERR", "DRR")):
            continue
        runs.append(SraRun(
            run=r["Run"], experiment=r["Experiment"], biosample=r["BioSample"],
            library_name=r.get("LibraryName") or None, library_strategy=r.get("LibraryStrategy") or None,
            library_selection=r.get("LibrarySelection") or None, library_source=r.get("LibrarySource") or None,
            library_layout=(r.get("LibraryLayout") or "").upper() or None, platform=r.get("Platform") or None,
            instrument_model=r.get("Model") or None, spots=_int(r.get("spots")), bases=_int(r.get("bases")),
            avg_read_len=_int(r.get("avgLength")), release_date=_date(r.get("ReleaseDate")),
        ))
    return runs


def subject_values(samples: list[BioSample], attr: str | None) -> dict[str, str]:
    """BioSample accession → value of the attribute that identifies the animal (absent ⇒ no key)."""
    if not attr:
        return {}
    out = {}
    for s in samples:
        v = s.get(attr)
        if not is_null(v):
            out[s.accession] = v
    return out


def validate(project: BioProject, samples: list[BioSample], runs: list[SraRun],
             subject_attr: str | None, constant_per_subject: tuple[str, ...]) -> Validation:
    """Refuse to load what doesn't add up. Errors stop the ingest; warnings are reported."""
    v = Validation()
    accs = [s.accession for s in samples]
    if not samples:
        v.errors.append(f"{project.accession}: no BioSamples linked")
    if len(set(accs)) != len(accs):
        v.errors.append("duplicate BioSample accessions in efetch result")
    if len({r.run for r in runs}) != len(runs):
        v.errors.append("duplicate run accessions in runinfo")
    known = set(accs)
    stray = sorted({r.biosample for r in runs} - known)
    if stray:
        v.errors.append(f"{len(stray)} runs point at BioSamples not linked to the project: {stray[:5]}")
    exp_sample: dict[str, set[str]] = defaultdict(set)
    for r in runs:
        exp_sample[r.experiment].add(r.biosample)
        if r.library_layout not in (None, "SINGLE", "PAIRED"):
            v.errors.append(f"{r.run}: library_layout {r.library_layout!r} is neither SINGLE nor PAIRED")
    for exp, bss in exp_sample.items():
        if len(bss) > 1:
            v.errors.append(f"{exp}: one SRA experiment spans BioSamples {sorted(bss)}")
    no_run = sorted(known - {r.biosample for r in runs})
    if no_run:
        v.warnings.append(f"{len(no_run)} BioSamples have no SRA run: {no_run[:5]}")

    if subject_attr:
        subj = subject_values(samples, subject_attr)
        missing = [a for a in accs if a not in subj]
        if missing:
            v.warnings.append(f"{len(missing)} BioSamples lack {subject_attr!r}; they get no subject: {missing[:5]}")
        by_subject: dict[str, list[BioSample]] = defaultdict(list)
        for s in samples:
            if s.accession in subj:
                by_subject[subj[s.accession]].append(s)
        for name in constant_per_subject:
            for key, members in by_subject.items():
                values = {m.get(name) for m in members}
                if len(values) > 1:
                    v.errors.append(f"subject {key!r}: {name!r} differs between its samples: {sorted(map(str, values))}")
    return v
