"""JATS sections and quote matching, on the real PMC8024495 full text. No network, no database."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.jats import find_quote, norm, sections

PAPER = Path(__file__).parent / "fixtures" / "europepmc" / "PMC8024495" / "fulltext.xml"


@pytest.fixture(scope="module")
def jats() -> bytes:
    return PAPER.read_bytes()


def test_norm_folds_typography_and_space():
    assert norm("5′-GTG  CA–3’  “x”") == "5'-GTG CA-3' \"x\""


def test_section_paths(jats):
    paths = {s.path for s in sections(jats)}
    assert ("Materials and Methods", "Statistical Analysis") in paths
    assert ("Results", "Intestinal Microbiota Population Characteristics", "Cecum") in paths


def test_parent_excludes_subsection_text(jats):
    by_path = {s.path: s for s in sections(jats)}
    parent = by_path[("Results", "Intestinal Microbiota Population Characteristics")]
    assert "9,280,410" in parent.text
    assert "2,707,536" not in parent.text          # that sentence is in the Cecum subsection


def test_quote_found_in_its_section(jats):
    hits = find_quote(jats, "Pigs were individually housed and fed ad libitum for 36 days",
                      "Animals, Diets, and Experimental Design")
    assert [h.title for h in hits] == ["Animals, Diets, and Experimental Design"]


def test_quote_with_ascii_quotes_matches_typographic_source(jats):
    assert find_quote(jats, "515F (5'-GTGYCAGCMGCCGCGGTAA-3'; 21)")


def test_formula_text_is_included(jats):
    assert find_quote(jats, "Yijkl=μ+τi+υj+ρk+eijkl", "Statistical Analysis")


def test_quote_in_wrong_section_is_not_found(jats):
    assert not find_quote(jats, "Pigs were individually housed", "Statistical Analysis")


def test_paraphrase_is_not_found(jats):
    assert not find_quote(jats, "Pigs were housed individually and fed ad libitum for 36 days")
