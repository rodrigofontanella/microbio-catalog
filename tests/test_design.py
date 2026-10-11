"""Design spec parsing and planning. No database.

Uses the real spec (conf/designs/PRJNA667518.yml) and the real BioSamples of pens 101 and 102.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.ingest.design_spec import DesignSpec, balance, load_spec, plan_design
from app.ingest.ncbi_parse import parse_biosamples

ROOT = Path(__file__).parents[1]
SPEC = ROOT / "conf" / "designs" / "PRJNA667518.yml"
FIX = Path(__file__).parent / "fixtures" / "ncbi" / "PRJNA667518" / "biosample.xml"


@pytest.fixture(scope="module")
def spec() -> DesignSpec:
    return load_spec(SPEC)


@pytest.fixture(scope="module")
def fixture_values() -> dict[str, dict[str, set[str]]]:
    """What design.subject_values() returns, built from the fixture's BioSample attributes."""
    out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for s in parse_biosamples(FIX.read_bytes()):
        for a in s.attributes:
            out[f"pen-{s.get('pen')}"][a.field].add(a.value)
    return {k: dict(v) for k, v in out.items()}


def synthetic(rows: list[tuple[str, str, str, str]]) -> dict[str, dict[str, set[str]]]:
    """(subject, treatment, rep, block) → subject_values shape."""
    return {s: {"treatment": {t}, "rep": {r}, "block": {b}} for s, t, r, b in rows}


def test_real_spec_loads(spec):
    assert spec.experiment.design_type == "randomized_block" and spec.experiment.experimental_unit == "pen"
    assert [f.name for f in spec.factors] == ["diet", "rep", "block"]
    assert spec.factors[2].nested_in == "rep"
    assert {k for k, v in spec.factors[0].levels.items() if v.control} == {"LF", "HF"}


def test_typo_in_spec_is_rejected():
    raw = yaml.safe_load(SPEC.read_text())
    raw["factors"][0]["nested"] = "rep"                   # not 'nested_in'
    with pytest.raises(ValidationError):
        DesignSpec.model_validate(raw)


def test_plan_on_real_pens(spec, fixture_values):
    plan = plan_design(spec, fixture_values)
    assert plan.ok, plan.errors
    assert set(plan.assignments) == {"pen-101", "pen-102"}
    a101 = plan.assignments["pen-101"]
    assert a101["rep"] == (None, "1") and a101["block"] == ("1", a101["block"][1])   # block keyed under its rep
    assert {lbl for _, lbl in plan.levels["diet"]} <= {"LF", "HF", "HF+XY", "HF+AX"}
    assert any("never used" in w for w in plan.warnings)  # 2 pens can't use all 4 diets


def test_same_block_label_in_two_reps_is_two_levels(spec):
    plan = plan_design(spec, synthetic([("p1", "LF", "1", "2"), ("p2", "LF", "3", "2"),
                                        ("p3", "HF", "1", "2"), ("p4", "HF+XY", "1", "1"),
                                        ("p5", "HF+AX", "1", "1")]))
    assert plan.ok
    assert {k for k in plan.levels["block"]} == {("1", "2"), ("3", "2"), ("1", "1")}


@pytest.mark.parametrize("values,message", [
    ({"p1": {"treatment": {"LF", "HF"}, "rep": {"1"}, "block": {"1"}}}, "needs one value"),
    ({"p1": {"treatment": {"XX"}, "rep": {"1"}, "block": {"1"}}}, "isn't a declared level"),
    ({"p1": {"treatment": {"LF"}, "block": {"1"}}}, "found none"),
])
def test_data_that_does_not_fit_the_spec_is_an_error(spec, values, message):
    plan = plan_design(spec, values)
    assert not plan.ok and any(message in e for e in plan.errors)


def test_nested_factor_must_follow_its_parent(spec):
    bad = spec.model_copy(update={"factors": [spec.factors[2], spec.factors[0], spec.factors[1]]})
    plan = plan_design(bad, synthetic([("p1", "LF", "1", "1")]))
    assert any("isn't declared before it" in e for e in plan.errors)


def test_balance_reports_a_complete_block_design(spec):
    rows = [(f"p{r}{b}{t}", t, str(r), str(b)) for r in (1, 2) for b in (1, 2)
            for t in ("LF", "HF", "HF+XY", "HF+AX")]
    lines = balance(spec, plan_design(spec, synthetic(rows)))
    assert "diet within block: 4 block levels → 4× {'HF': 1, 'HF+AX': 1, 'HF+XY': 1, 'LF': 1}" in lines
    assert "diet within rep: 2 rep levels → 2× {'HF': 2, 'HF+AX': 2, 'HF+XY': 2, 'LF': 2}" in lines
