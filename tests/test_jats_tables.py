"""JATS tables cell by cell, on the real PMC8001633 full text (study 2's paper). No network, no database."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.ingest.jats import (find_quote, find_rows, parse_df, parse_mean_spread, parse_number, parse_p, sections,
                             tables)

PAPER = Path(__file__).parent / "fixtures" / "europepmc" / "PMC8001633" / "fulltext.xml"
RESULTS_BETA = "3.2. Effects of Treatment and Gut Region on Beta Diversity"


@pytest.fixture(scope="module")
def jats() -> bytes:
    return PAPER.read_bytes()


@pytest.fixture(scope="module")
def tabs(jats):
    return tables(jats)


def test_why_tables_need_their_own_check(jats):
    # the cells sit in the section text run together, so a bare number "verifies" against Table 3
    sec = next(s for s in sections(jats) if s.title == RESULTS_BETA)
    assert "CaecumUnweighted UniFrac2.151.56 0.007" in sec.text
    assert find_quote(jats, "0.007", RESULTS_BETA)


def test_four_tables_by_label(tabs):
    assert sorted(tabs) == ["Table 1", "Table 2", "Table 3", "Table 4"]
    assert tabs["Table 2"].caption.startswith("General Linear Models")      # but it reports PERMANOVA pseudo-F


def test_break_is_a_space_and_blank_header_is_named(tabs):
    assert tabs["Table 2"].header[0] == "β-Diversity Distance Matrix"         # <break/> between the words
    assert tabs["Table 3"].header == ("col1", "β-Diversity Distance Matrix", "D.f.", "Pseudo-F", "p")


def test_rowspan_is_expanded(tabs):
    t1 = tabs["Table 1"]
    assert [r["Alpha Diversity Index"] for r in t1.rows[:3]] == ["Species richness"] * 3
    assert [r["Explanatory Variables"] for r in t1.rows[:3]] == ["Treatment", "Gut Region", "Gut Region × Treatment"]


def test_blank_cells_stay_blank(tabs):
    weighted_caecum = tabs["Table 3"].rows[5]
    assert weighted_caecum["col1"] == "" and weighted_caecum["β-Diversity Distance Matrix"] == "Weighted UniFrac"


def test_find_rows_fills_down_for_lookup_only(tabs):
    t3 = tabs["Table 3"]
    hit = find_rows(t3, {"col1": "Caecum", "β-Diversity Distance Matrix": "Weighted UniFrac"})
    assert hit == [5] and t3.rows[5]["p"] == "0.191"
    assert len(find_rows(t3, {"col1": "Caecum"})) == 2                       # a lookup must name one row


def test_find_rows_folds_dashes(tabs):
    # the source has an en dash ("28–70"); a spec may type a hyphen
    hit = find_rows(tabs["Table 4"], {"Dependent Variable": "ADG 28-70 days, g/d", "Independent Variables": "Treatment"})
    assert len(hit) == 1 and tabs["Table 4"].rows[hit[0]]["Control"] == "325.25 (11.01)"


def test_unknown_column_matches_nothing(tabs):
    assert find_rows(tabs["Table 3"], {"Site": "Caecum"}) == []


def test_df_printed_without_its_comma():
    assert parse_df("2.61") == (2, 61) and parse_df("2, 18") == (2, 18) and parse_df("1.19") == (1, 19)
    for f_value in ("4.03", "0.25", "1.06"):           # F / pseudo-F cells of Tables 1-3: not d.f. pairs
        with pytest.raises(ValueError):
            parse_df(f_value)
    with pytest.raises(ValueError):
        parse_df("2")


def test_p_with_and_without_operator():
    assert parse_p("<0.001") == ("<", Decimal("0.001"))
    assert parse_p("0.023") == ("=", Decimal("0.023"))
    with pytest.raises(ValueError):
        parse_p("n.s.")


def test_statistic_with_operator_is_refused(tabs):
    initial_bw = tabs["Table 4"].rows[0]
    assert initial_bw["F"] == "<0.01"
    with pytest.raises(ValueError, match="operator"):
        parse_number(initial_bw["F"])
    assert parse_number("14.59") == Decimal("14.59")


def test_mean_and_spread(tabs):
    assert parse_mean_spread("325.25 (11.01)") == (Decimal("325.25"), Decimal("11.01"))
    assert parse_mean_spread("7.34") == (Decimal("7.34"), None)
    assert parse_mean_spread(tabs["Table 1"].rows[0]["Antibiotic"]) == (Decimal("294.14"), Decimal("41.60"))
    with pytest.raises(ValueError):
        parse_mean_spread("325.25 ± 11.01")
