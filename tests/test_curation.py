"""Curation spec and its evidence guard, on the real spec and the real paper. No database.

Also covers the findings draft checker (scripts/check_findings_quotes.py).
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.ingest.curation import CurationSpec, Fact, check_evidence, load_spec
from scripts.check_findings_quotes import check as check_findings

ROOT = Path(__file__).parents[1]
SPEC = ROOT / "conf" / "curation" / "PRJNA667518.yml"
DRAFT = ROOT / "conf" / "findings" / "PRJNA667518_draft.yml"
DESIGN = ROOT / "conf" / "designs" / "PRJNA667518.yml"
PAPER = Path(__file__).parent / "fixtures" / "europepmc" / "PMC8024495" / "fulltext.xml"


@pytest.fixture(scope="module")
def jats() -> bytes:
    return PAPER.read_bytes()


@pytest.fixture(scope="module")
def raw() -> dict:
    return yaml.safe_load(SPEC.read_text())


def test_real_spec_passes_the_guard(jats):
    assert check_evidence(load_spec(SPEC), jats) == []


def test_real_spec_is_signed():
    # signed after review; a curated load refuses an unsigned spec
    assert load_spec(SPEC).review.reviewed_by == "Rodrigo Fontanella"


def _with_fact(raw: dict, fact_id: str, **changes) -> CurationSpec:
    r = copy.deepcopy(raw)
    f = next(x for x in r["facts"] if x["id"] == fact_id)
    f.update(changes)
    return CurationSpec.model_validate(r)


def test_paraphrased_quote_fails(raw, jats):
    spec = _with_fact(raw, "one-pig-per-pen", quote="Pigs were housed individually for 36 days")
    assert any("quote not found" in e for e in check_evidence(spec, jats))


def test_unknown_section_fails(raw, jats):
    spec = _with_fact(raw, "one-pig-per-pen", section="Animals")
    assert any("no section titled 'Animals'" in e for e in check_evidence(spec, jats))


def test_not_reported_fails_when_the_word_occurs(raw, jats):
    absent = {"sections": ["Animals, Diets, and Experimental Design"], "patterns": [r"individually"]}
    spec = _with_fact(raw, "arrive-5-blinding", absent=absent)
    assert any("'not reported' but" in e for e in check_evidence(spec, jats))


def test_target_names_exactly_one_entity():
    with pytest.raises(ValidationError, match="exactly one"):
        Fact.model_validate({"id": "x", "target": {"study": True, "document": True}, "field": "f",
                             "value_text": "v", "section": "s", "quote": "q"})


def test_fact_needs_quote_or_absent_not_both():
    with pytest.raises(ValidationError, match="either a quote"):
        Fact.model_validate({"id": "x", "target": {"study": True}, "field": "f", "value_text": "v",
                             "section": "s", "quote": "q", "absent": {"sections": ["s"], "patterns": ["p"]}})


def test_typo_in_spec_is_rejected(raw):
    r = copy.deepcopy(raw)
    r["facts"][0]["qoute"] = "x"
    with pytest.raises(ValidationError):
        CurationSpec.model_validate(r)


def test_duplicate_fact_ids_are_rejected(tmp_path, raw):
    r = copy.deepcopy(raw)
    r["facts"].append(r["facts"][0])
    p = tmp_path / "dup.yml"
    p.write_text(yaml.safe_dump(r))
    with pytest.raises(ValueError, match="duplicate fact ids"):
        load_spec(p)


# ---- findings draft ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def draft() -> dict:
    return yaml.safe_load(DRAFT.read_text())


@pytest.fixture(scope="module")
def design() -> dict:
    return yaml.safe_load(DESIGN.read_text())


def test_findings_draft_passes(draft, jats, design):
    assert check_findings(draft, jats, design) == []


def test_findings_draft_catches_bad_quote_level_and_effect(draft, jats, design):
    d = copy.deepcopy(draft)
    d["claims"][0]["quote"] = "HF reduced Lactobacillus"
    d["claims"][1]["contrast"]["treatment"] = "HF+XYZ"
    d["claims"][2]["effect"] = {"log2fc": 1.2}
    errors = check_findings(d, jats, design)
    assert len(errors) == 3, errors
