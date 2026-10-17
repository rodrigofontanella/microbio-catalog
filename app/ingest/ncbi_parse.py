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


@dataclass(frozen=True)
class SubjectRule:
    """How a BioSample names its animal.

    attr only: the attribute's value *is* the animal key (PRJNA667518's `Pen`).
    attr + pattern: the key is the pattern's named group `animal` (PRJNA664026's `isolate` = tissue code + animal
    number, e.g. 'Ci54'). An optional group `code` is checked against `code_map` on `code_attr`: a code that
    disagrees with the sample's own attribute stops the load, because then the string isn't what we think it is.
    """
    attr: str
    pattern: re.Pattern[str] | None = None
    code_attr: str | None = None
    code_map: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_cli(cls, attr: str | None, pattern: str | None, code_map: str | None) -> "SubjectRule":
        """attr: 'Pen'; pattern: 'isolate=^(?P<code>Ci|I|Co|D)(?P<animal>[0-9]+)$';
        code_map: 'tissue:Ci=Caecum,I=Ileum,Co=Colon,D=Duodenum'."""
        if pattern:
            if attr:
                raise ValueError("give --subject-attr or --subject-pattern, not both")
            attr, _, rx = pattern.partition("=")
            if not attr or not rx:
                raise ValueError(f"--subject-pattern must be ATTR=REGEX, got {pattern!r}")
            compiled = re.compile(rx)
            if "animal" not in compiled.groupindex:
                raise ValueError(f"--subject-pattern needs a named group (?P<animal>…): {rx!r}")
        else:
            compiled = None
        code_attr, pairs = None, ()
        if code_map:
            if compiled is None or "code" not in compiled.groupindex:
                raise ValueError("--subject-code-map needs a --subject-pattern with a named group (?P<code>…)")
            code_attr, _, body = code_map.partition(":")
            pairs = tuple(tuple(x.split("=", 1)) for x in body.split(","))
            if not code_attr or not pairs or any(len(p) != 2 or not p[0] or not p[1] for p in pairs):
                raise ValueError(f"--subject-code-map must be ATTR:CODE=VALUE,…, got {code_map!r}")
        return cls(attr=(attr or "host_subject_id").strip(), pattern=compiled,
                   code_attr=code_attr.strip() if code_attr else None, code_map=pairs)

    @property
    def is_parsed(self) -> bool:
        return self.pattern is not None

    def describe(self) -> dict:
        return {"attr": self.attr, "pattern": self.pattern.pattern if self.pattern else None,
                "code_attr": self.code_attr, "code_map": dict(self.code_map)}

    def match(self, s: BioSample) -> tuple[str | None, str | None, str | None]:
        """(animal key, code, raw value); key None if the attribute is absent or doesn't match."""
        v = s.get(self.attr)
        if is_null(v):
            return None, None, None
        if self.pattern is None:
            return v, None, v
        m = self.pattern.fullmatch(v)
        if not m:
            return None, None, v
        return m["animal"], (m["code"] if "code" in m.groupdict() else None), v


def _rule(subject: "SubjectRule | str | None") -> "SubjectRule | None":
    if subject is None or isinstance(subject, SubjectRule):
        return subject
    return SubjectRule(attr=subject)


def subject_values(samples: list[BioSample], subject: "SubjectRule | str | None") -> dict[str, str]:
    """BioSample accession → animal key (absent or unparsable ⇒ no key; validate() reports those)."""
    rule = _rule(subject)
    if rule is None:
        return {}
    out = {}
    for s in samples:
        key, _, _ = rule.match(s)
        if key is not None:
            out[s.accession] = key
    return out


def validate(project: BioProject, samples: list[BioSample], runs: list[SraRun],
             subject: "SubjectRule | str | None", constant_per_subject: tuple[str, ...]) -> Validation:
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

    rule = _rule(subject)
    if rule:
        subj = subject_values(samples, rule)
        missing = [s.accession for s in samples if is_null(s.get(rule.attr))]
        if missing:
            v.warnings.append(f"{len(missing)} BioSamples lack {rule.attr!r}; they get no subject: {missing[:5]}")
        if rule.is_parsed:
            # a parsed key is an inference about a string: every value must parse, and codes must agree
            unparsed = [(s.accession, rule.match(s)[2]) for s in samples
                        if not is_null(s.get(rule.attr)) and s.accession not in subj]
            if unparsed:
                v.errors.append(f"{len(unparsed)} {rule.attr!r} values don't match {rule.pattern.pattern!r}: {unparsed[:5]}")
            codes = dict(rule.code_map)
            seen: dict[tuple[str, str], str] = {}
            for s in samples:
                key, code, raw = rule.match(s)
                if key is None or code is None:
                    continue
                if codes:
                    want, got = codes.get(code), s.get(rule.code_attr)
                    if want is None:
                        v.errors.append(f"{s.accession}: {rule.attr} {raw!r} has code {code!r}, not in the code map")
                    elif got != want:
                        v.errors.append(f"{s.accession}: {rule.attr} {raw!r} says {rule.code_attr} {want!r}, "
                                        f"the sample says {got!r}")
                if (key, code) in seen:
                    v.errors.append(f"animal {key!r} has code {code!r} twice: {seen[(key, code)]}, {s.accession}")
                seen[(key, code)] = s.accession
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
