"""Load reviewed facts from a paper (conf/curation/<BioProject>.yml) as tier 'curated' assertions.

    python -m app.ingest.curation conf/curation/PRJNA667518.yml --dry-run     # checks; review not needed
    python -m app.ingest.curation conf/curation/PRJNA667518.yml               # needs review.reviewed_by

Two guards make 'curated' mean something:
  1. Every quote must appear verbatim (whitespace and typographic quotes folded) in the named section
     of the paper's full text, read from the raw file recorded on rag.document and checked against its
     sha256. A paraphrase or an invented quote fails the load.
  2. A real load requires `review.reviewed_by` and `review.reviewed_on`: a person signed the file.
"Not reported" facts carry no quote; instead the listed patterns must be ABSENT from the listed sections.
Targets: the study, the experiment, the paper itself, all subjects, all libraries, or the samples
matching one BioSample field value.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.ingest.jats import find_quote, norm, sections
from app.ingest.ncbi_study import code_version
from app.models import (Assertion, Document, Experiment, IngestRun, Library, Sample, Study, StudySample,
                        Subject)

TARGETS = ("study_id", "subject_id", "sample_id", "library_id", "run_acc", "genome_acc", "experiment_id", "doc_id")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SampleSelector(_Strict):
    field: str          # a BioSample assertion field, e.g. env_local_scale
    equals: str


class Target(_Strict):
    study: bool = False
    experiment: str | None = None          # experiment local_id
    document: bool = False                 # the paper itself
    subjects: Literal["all"] | None = None
    libraries: Literal["all"] | None = None
    samples: SampleSelector | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> "Target":
        n = sum(bool(x) for x in (self.study, self.experiment, self.document, self.subjects, self.libraries,
                                  self.samples))
        if n != 1:
            raise ValueError("a target names exactly one of study / experiment / document / subjects / "
                             "libraries / samples")
        return self


class Absent(_Strict):
    sections: list[str]                    # section titles searched
    patterns: list[str]                    # case-insensitive regexes that must NOT occur there


class Fact(_Strict):
    id: str
    target: Target
    field: str
    value_text: str | None = None
    value_num: float | None = None
    unit: str | None = None
    section: str | None = None             # section title the quote is in
    quote: str | None = None
    absent: Absent | None = None
    note: str | None = None

    @model_validator(mode="after")
    def evidence_shape(self) -> "Fact":
        if self.value_text is None and self.value_num is None:
            raise ValueError(f"{self.id}: a fact needs value_text or value_num")
        if (self.quote is None) == (self.absent is None):
            raise ValueError(f"{self.id}: give either a quote (+ section) or an 'absent' check, not both/neither")
        if self.quote is not None and not self.section:
            raise ValueError(f"{self.id}: a quote needs the section it is in")
        return self


class Review(_Strict):
    reviewed_by: str | None = None
    reviewed_on: date | None = None


class CurationSpec(_Strict):
    bioproject: str
    pmcid: str
    method: str
    review: Review
    facts: list[Fact]


def load_spec(path: Path) -> CurationSpec:
    spec = CurationSpec.model_validate(yaml.safe_load(path.read_text()))
    ids = [f.id for f in spec.facts]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate fact ids: {dupes}")
    return spec


def check_evidence(spec: CurationSpec, jats: bytes) -> list[str]:
    """Errors: quotes not found in their section; 'absent' patterns that do occur."""
    errors = []
    secs = sections(jats)
    titles = {s.title for s in secs}
    for f in spec.facts:
        if f.quote is not None:
            if norm(f.section) not in titles:
                errors.append(f"{f.id}: no section titled {f.section!r}")
            elif not find_quote(jats, f.quote, f.section):
                errors.append(f"{f.id}: quote not found verbatim in {f.section!r}: {f.quote[:80]!r}")
        else:
            for title in f.absent.sections:
                if norm(title) not in titles:
                    errors.append(f"{f.id}: no section titled {title!r}")
                    continue
                text = " ".join(s.text for s in secs if s.title == norm(title))
                for pat in f.absent.patterns:
                    m = re.search(pat, text, re.IGNORECASE)
                    if m:
                        errors.append(f"{f.id}: 'not reported' but {pat!r} occurs in {title!r}: …{m.group(0)}…")
    return errors


async def resolve(db, f: Fact, study_id: int, doc_id: int) -> list[dict]:
    """The assertion target column(s) for one fact."""
    t = f.target
    if t.study:
        return [{"study_id": study_id}]
    if t.document:
        return [{"doc_id": doc_id}]
    if t.experiment:
        eid = (await db.execute(select(Experiment.experiment_id).where(
            Experiment.study_id == study_id, Experiment.local_id == t.experiment))).scalar_one_or_none()
        if eid is None:
            raise LookupError(f"{f.id}: experiment {t.experiment!r} not loaded (run app.ingest.design first)")
        return [{"experiment_id": eid}]
    if t.subjects:
        ids = (await db.execute(select(Subject.subject_id).where(Subject.study_id == study_id))).scalars().all()
        return [{"subject_id": i} for i in ids]
    if t.libraries:
        ids = (await db.execute(select(Library.library_id).where(Library.study_id == study_id))).scalars().all()
        return [{"library_id": i} for i in ids]
    ids = (await db.execute(
        select(Sample.sample_id).join(StudySample, StudySample.sample_id == Sample.sample_id)
        .join(Assertion, Assertion.sample_id == Sample.sample_id)
        .where(StudySample.study_id == study_id, Assertion.field == t.samples.field,
               Assertion.value_text == t.samples.equals, Assertion.method.like("biosample_attr:%"))
        .distinct())).scalars().all()
    return [{"sample_id": i} for i in ids]


async def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingest.curation", description=__doc__.splitlines()[0])
    ap.add_argument("spec", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    raw_spec = args.spec.read_bytes()
    spec = load_spec(args.spec)
    reviewed = bool(spec.review.reviewed_by and spec.review.reviewed_on)
    if not reviewed and not args.dry_run:
        print("ERROR review.reviewed_by / reviewed_on are empty: a curated load needs a signed review "
              "(--dry-run checks the evidence without it)", file=sys.stderr)
        return 1

    from app.database import AsyncSessionLocal
    counts: Counter = Counter()
    async with AsyncSessionLocal() as db:
        study_id = (await db.execute(select(Study.study_id).where(Study.bioproject_acc == spec.bioproject))
                    ).scalar_one_or_none()
        doc = (await db.execute(select(Document).where(Document.pmcid == spec.pmcid))).scalar_one_or_none()
        if study_id is None or doc is None:
            print(f"ERROR load the study ({spec.bioproject}) and the paper ({spec.pmcid}) first", file=sys.stderr)
            return 1
        xml_path = Path(doc.raw_uri) / "fulltext.xml"
        jats = xml_path.read_bytes()
        if hashlib.sha256(jats).hexdigest() != doc.raw_sha256.get("fulltext.xml"):
            print(f"ERROR {xml_path} no longer matches the sha256 recorded on rag.document", file=sys.stderr)
            return 1
        errors = check_evidence(spec, jats)
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        if errors:
            return 1
        print(f"{len(spec.facts)} facts: every quote found verbatim, every 'not reported' check passed",
              file=sys.stderr)

        params = {"spec": str(args.spec), "spec_sha256": hashlib.sha256(raw_spec).hexdigest(),
                  "reviewed_by": spec.review.reviewed_by, "reviewed_on": str(spec.review.reviewed_on),
                  "pmcid": spec.pmcid, "doc_id": doc.doc_id, "dry_run": args.dry_run, **code_version()}
        ir = IngestRun(source="manual", source_release=f"curation:{args.spec.name}", params=params,
                       raw_uri=str(args.spec))
        db.add(ir)
        await db.flush()

        existing = set()
        rows = []
        for f in spec.facts:
            try:
                targets = await resolve(db, f, study_id, doc.doc_id)
            except LookupError as e:
                await db.rollback()
                print(f"ERROR {e}", file=sys.stderr)
                return 1
            if not targets:
                await db.rollback()
                print(f"ERROR {f.id}: target matches nothing", file=sys.stderr)
                return 1
            for t in targets:
                col, key = next(iter(t.items()))
                c = getattr(Assertion, col)
                hit = (await db.execute(select(Assertion.assertion_id).where(
                    c == key, Assertion.field == f.field, Assertion.method == spec.method,
                    Assertion.tier == "curated",
                    Assertion.value_text.is_not_distinct_from(f.value_text),
                    Assertion.value_num.is_not_distinct_from(f.value_num),
                    Assertion.unit.is_not_distinct_from(f.unit))
                    .limit(1))).first()
                if hit or (col, key, f.field, f.value_text, f.value_num) in existing:
                    counts["assertion.existing"] += 1
                    continue
                existing.add((col, key, f.field, f.value_text, f.value_num))
                rows.append({**{k: None for k in TARGETS}, **t, "field": f.field, "value_text": f.value_text,
                             "value_num": f.value_num, "unit": f.unit, "value_term": None, "tier": "curated",
                             "method": spec.method, "ingest_run_id": ir.ingest_run_id,
                             "evidence": {"fact_id": f.id, "pmcid": spec.pmcid, "doc_id": doc.doc_id,
                                          "section": f.section, "quote": f.quote,
                                          "absent": f.absent.model_dump() if f.absent else None,
                                          "note": f.note, "reviewed_by": spec.review.reviewed_by,
                                          "reviewed_on": str(spec.review.reviewed_on),
                                          "fulltext_sha256": doc.raw_sha256.get("fulltext.xml")}})
            counts[f"target.{next(iter(targets[0]))}"] += len(targets)
        for i in range(0, len(rows), 1000):
            await db.execute(pg_insert(Assertion).values(rows[i:i + 1000]))
        counts["assertion.new"] = len(rows)
        await db.execute(update(IngestRun).where(IngestRun.ingest_run_id == ir.ingest_run_id).values(
            finished_at=datetime.now(timezone.utc), params={**params, "counts": dict(counts)}))
        irid = ir.ingest_run_id
        await (db.rollback() if args.dry_run else db.commit())
    verb = "dry run, rolled back" if args.dry_run else "committed"
    print(f"ingest_run {irid} ({verb}): " + json.dumps(dict(sorted(counts.items()))), file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
