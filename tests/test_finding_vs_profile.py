"""Paper findings vs a profile (app.reports.finding_vs_profile): matching, statistics, verdicts, depth scenarios.
Made-up lineages and counts in the shapes PRJNA430317 showed (bracket-stripped Greengenes names); no Spark, no
database."""
from __future__ import annotations

from pathlib import Path

import pytest
from scipy.stats import false_discovery_control, kruskal

from app.lake.lineage import taxon_row
from app.reports import finding_vs_profile as fvp
from app.reports.finding_vs_profile import (Claim, ClaimMap, MatchSpec, RunInfo, analyse, check_coverage,
                                            check_profile, markdown, rel_abundance, resolve, scenario, site_runs,
                                            site_tests, tsv_rows, units, verdict)

PREV = "Bacteria;Bacteroidetes;Bacteroidia;Bacteroidales;Prevotellaceae;Prevotella"
PARA = "Bacteria;Bacteroidetes;Bacteroidia;Bacteroidales;Paraprevotellaceae;Prevotella"
BLAU = "Bacteria;Firmicutes;Clostridia;Clostridiales;Lachnospiraceae;Blautia"
LACH = "Bacteria;Firmicutes;Clostridia;Clostridiales;Lachnospiraceae;__"
ASTE = "Bacteria;Tenericutes;Erysipelotrichi;Erysipelotrichales;Erysipelotrichaceae;Asteroleplasma"
FEATURES = [f"{PREV};copri", f"{PREV};__", f"{PARA};__", f"{BLAU};__", f"{LACH};__", f"{ASTE};__"]
TAXA = {fid: taxon_row(fid) for fid in FEATURES}
CAE = "UBERON:0001153"
FEC = "UBERON:0001988"


def _spec(claims: dict, **kw) -> MatchSpec:
    return MatchSpec.model_validate({"bioproject": "PRJNA0000003", "pmcid": "PMC0000001", "profile_source": "pgmd",
                                     "review": {}, "sites": {CAE: {"env_local_scale": "caecum"},
                                                             FEC: {"env_local_scale": "feces"}},
                                     "claims": claims, **kw})


def _runs(site: str, statuses: dict[str, list[str]]) -> list[RunInfo]:
    """One run per pig: statuses = diet → depth status per pig."""
    out = []
    for diet, sts in statuses.items():
        for i, st in enumerate(sts):
            out.append(RunInfo(f"SRR{site}{diet[0]}{i}", f"{diet}/{i}", diet, {"env_local_scale": site}, st, None))
    return out


# ── the map ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_real_map_parses():
    spec = fvp.load_spec(Path(__file__).parents[1] / "conf/match/PRJNA430317.yml")
    assert len(spec.claims) == 26 and spec.level_check_field == "chem_administration"
    assert sum(1 for m in spec.claims.values() if m.not_testable) == 4
    assert spec.claims["g-cae-prevotella"].also == [{"family": "Paraprevotellaceae", "genus": "Prevotella"}]


def test_signed_needs_both_review_lines():
    assert not _spec({}).signed
    assert not _spec({}, review={"reviewed_by": "R"}).signed
    assert _spec({}, review={"reviewed_by": "R", "reviewed_on": "2026-11-10"}).signed


@pytest.mark.parametrize("entry, msg", [
    ({"taxon": {"genus": "X"}, "not_testable": "why"}, "exactly one"),
    ({}, "exactly one"),
    ({"not_testable": "why", "also": [{"genus": "X"}]}, "'also' needs a taxon"),
    ({"taxon": {"genera": "X"}}, "ranks from"),
    ({"taxon": {}}, "ranks from"),
])
def test_claim_map_shapes_are_refused(entry, msg):
    with pytest.raises(ValueError, match=msg):
        ClaimMap.model_validate(entry)


def test_coverage_stops_on_unlisted_unknown_unmapped_site_or_rank_mismatch():
    c = Claim("g-x", "genus", "Blautia", CAE, "increase", "antibiotics", "control")
    with pytest.raises(ValueError, match=r"unlisted \['g-x'\], unknown \['g-y'\]"):
        check_coverage(_spec({"g-y": {"taxon": {"genus": "Blautia"}}}), [c])
    with pytest.raises(ValueError, match="no sample selector"):
        check_coverage(_spec({"g-x": {"taxon": {"genus": "Blautia"}}}),
                       [Claim("g-x", "genus", "Blautia", "UBERON:0001155", "increase", "a", "c")])
    with pytest.raises(ValueError, match="mapped to a phylum selector"):
        check_coverage(_spec({"g-x": {"taxon": {"phylum": "Firmicutes"}}}), [c])


# ── matching ─────────────────────────────────────────────────────────────────────────────────────────────

def test_a_bare_name_under_two_families_is_ambiguous_not_summed():
    with pytest.raises(ValueError, match="ambiguous"):
        resolve({"genus": "Prevotella"}, TAXA)
    assert resolve({"family": "Prevotellaceae", "genus": "Prevotella"}, TAXA) == PREV
    assert resolve({"family": "Paraprevotellaceae", "genus": "Prevotella"}, TAXA) == PARA
    assert resolve({"phylum": "Firmicutes"}, TAXA) == "Bacteria;Firmicutes"


def test_a_selector_that_matches_nothing_stops():
    with pytest.raises(ValueError, match="matches nothing"):
        resolve({"family": "Ruminococcaceae", "genus": "Oscillibacter"}, TAXA)


def test_genus_units_keep_the_remainder_and_skip_family_remainders():
    u = units(TAXA, "genus")
    assert u[f"{PREV};copri"] == u[f"{PREV};__"] == PREV          # species + Genus;__ remainder → one genus
    assert f"{LACH};__" not in u                                   # no genus: not in the genus family
    assert units(TAXA, "phylum")[f"{LACH};__"] == "Bacteria;Firmicutes"


def test_relative_abundance_uses_every_row_of_the_run_as_denominator():
    rows = [("R1", f"{PREV};copri", 30), ("R1", f"{PREV};__", 10), ("R1", f"{LACH};__", 60)]
    rel = rel_abundance(rows, units(TAXA, "genus"))
    assert rel == {"R1": {PREV: pytest.approx(0.4)}}               # (30 + 10) / 100; LACH counts in the total


# ── statistics and verdicts ──────────────────────────────────────────────────────────────────────────────

def _rel(t: list[float], r: list[float], unit: str = PREV) -> tuple[dict, dict]:
    rel = {f"T{i}": {unit: v} for i, v in enumerate(t)} | {f"R{i}": {unit: v} for i, v in enumerate(r)}
    diet = {k: ("antibiotics" if k[0] == "T" else "control") for k in rel}
    return rel, diet


def test_separated_groups_are_tested_with_kw_and_bh():
    t = [0.01 * i for i in range(1, 10)]
    r = [0.2 + 0.01 * i for i in range(1, 10)]
    rel, diet = _rel(t, r)
    rel["T0"][BLAU] = rel["R0"][BLAU] = rel["R1"][BLAU] = 0.05     # a second unit, 3 pigs: at the threshold
    res = site_tests(rel, diet, "antibiotics", "control")
    x = res[PREV]
    assert (x.n_t, x.n_r, x.present_t, x.present_r) == (9, 9, 9, 9)
    assert x.direction == "decrease" and x.p == pytest.approx(kruskal(t, r).pvalue)
    ps = [res[PREV].p, res[BLAU].p]
    assert [res[PREV].q, res[BLAU].q] == pytest.approx(list(false_discovery_control(ps, method="bh")))


def test_prevalence_counts_both_diets_together():
    rel, diet = _rel([0.0] * 9, [0.1, 0.1, 0.1] + [0.0] * 6)       # gone under treatment: the signal, kept
    assert site_tests(rel, diet, "antibiotics", "control")[PREV].q is not None
    rel, diet = _rel([0.0] * 9, [0.1, 0.1] + [0.0] * 7)            # 2 pigs: below prevalence, listed, not tested
    x = site_tests(rel, diet, "antibiotics", "control")[PREV]
    assert x.p is None and x.q is None and x.present_r == 2


@pytest.mark.parametrize("claim, direction, q, expected", [
    ("decrease", "decrease", 0.01, "reproduced"),
    ("decrease", "decrease", 0.20, "direction_only"),
    ("decrease", "increase", 0.01, "contradicted"),
    ("decrease", "increase", 0.20, "opposite_ns"),
    ("increase", "none", 0.90, "no_direction"),
    ("no_difference", "increase", 0.20, "consistent_null"),
    ("no_difference", "decrease", 0.01, "contradicted"),
])
def test_verdicts(claim, direction, q, expected):
    assert verdict(claim, fvp.UnitTest(PREV, 9, 9, 9, 9, 0, 0, direction, 0.001, q)) == expected


def test_no_verdict_without_a_q_value():
    assert verdict("increase", None) == "not_testable"
    assert verdict("increase", fvp.UnitTest(PREV, 9, 9, 1, 1, 0, 0)) == "not_testable"


# ── runs and depth scenarios ─────────────────────────────────────────────────────────────────────────────

def test_two_runs_of_one_pig_at_a_site_stop():
    runs = _runs("caecum", {"antibiotics": ["ok"], "control": ["ok"]})
    runs.append(RunInfo("SRRdup", "antibiotics/0", "antibiotics", {"env_local_scale": "caecum"}, "ok", None))
    with pytest.raises(ValueError, match="pseudo-replicates"):
        site_runs(runs, {"env_local_scale": "caecum"}, ("antibiotics", "control"))


def test_scenarios_na_same_and_kept():
    lv = ("antibiotics", "control")
    feces = _runs("feces", {"antibiotics": ["ok", "ok"] + ["low"] * 6 + ["fail"], "control": ["low"] * 9})
    assert scenario(feces, "ok_only", lv) == "n/a"                  # control keeps 0 pigs
    assert len(scenario(feces, "no_fail", lv)) == 17
    caecum = _runs("caecum", {"antibiotics": ["ok"] * 6 + ["low"] * 3, "control": ["ok"] * 7 + ["low"] * 2})
    assert scenario(caecum, "no_fail", lv) == "= all"
    assert len(scenario(caecum, "ok_only", lv)) == 13
    assert len(scenario(caecum, "all", lv)) == 18


# ── end to end on made-up numbers ────────────────────────────────────────────────────────────────────────

def _study():
    runs = (_runs("caecum", {"antibiotics": ["ok"] * 6 + ["low"] * 3, "control": ["ok"] * 7 + ["low"] * 2})
            + _runs("feces", {"antibiotics": ["ok", "ok"] + ["low"] * 6 + ["fail"], "control": ["low"] * 9}))
    rows = []
    for r in runs:
        treated = r.diet == "antibiotics"
        i = int(r.run_acc[-1])
        rows += [(r.run_acc, f"{PREV};copri", (5 if treated else 40) + i), (r.run_acc, f"{PARA};__", 10 + i),
                 (r.run_acc, f"{LACH};__", 100), (r.run_acc, f"{BLAU};__", 20 + (i % 3))]
    claims = [Claim("g-cae-prevotella", "genus", "Prevotella", CAE, "decrease", "antibiotics", "control", "Q < 0.05"),
              Claim("g-cae-paraprevotella", "genus", "Paraprevotella", CAE, "increase", "antibiotics", "control"),
              Claim("b-feces-firmicutes-null", "phylum", "Firmicutes", FEC, "no_difference", "antibiotics", "control")]
    spec = _spec({"g-cae-prevotella": {"taxon": {"family": "Prevotellaceae", "genus": "Prevotella"},
                                       "also": [{"family": "Paraprevotellaceae", "genus": "Prevotella"}]},
                  "g-cae-paraprevotella": {"not_testable": "no such genus in the profile"},
                  "b-feces-firmicutes-null": {"taxon": {"phylum": "Firmicutes"}}})
    return spec, claims, runs, rows


def test_analyse_end_to_end():
    spec, claims, runs, rows = _study()
    out, families = analyse(spec, claims, runs, rows, TAXA)
    got = {(r.claim.local_id, r.role, r.scenario): r for r in out}
    assert got[("g-cae-prevotella", "verdict", "all")].verdict == "reproduced"
    assert got[("g-cae-prevotella", "verdict", "no_fail")].verdict == "= all"
    assert got[("g-cae-prevotella", "verdict", "ok_only")].verdict == "reproduced"
    assert got[("g-cae-prevotella", "also", "all")].verdict == "(no verdict)"
    assert got[("g-cae-prevotella", "also", "all")].mapped == PARA
    assert got[("g-cae-paraprevotella", "verdict", "all")].verdict == "not_testable"
    assert got[("b-feces-firmicutes-null", "verdict", "ok_only")].verdict == "n/a"
    assert got[("b-feces-firmicutes-null", "verdict", "no_fail")].res.n_t == 8
    # the caecum genus family: Prevotellaceae;Prevotella, Paraprevotellaceae;Prevotella, Blautia (not LACH)
    fam = next(f for f in families if f.site_term == CAE and f.rank == "genus" and f.scenario == "all")
    assert (fam.n_t, fam.n_r, fam.tested) == (9, 9, 3)


def test_profile_check_stops_on_total_or_run_mismatch():
    spec, claims, runs, rows = _study()
    totals: dict[str, int] = {}
    for run, _, c in rows:
        totals[run] = totals.get(run, 0) + c
    good = [RunInfo(r.run_acc, r.subject, r.diet, r.fields, r.depth_status, totals[r.run_acc]) for r in runs]
    check_profile(rows, TAXA, good)
    bad = good[:1] + [RunInfo(good[1].run_acc, good[1].subject, good[1].diet, good[1].fields, "ok", 1)] + good[2:]
    with pytest.raises(ValueError, match="n_reads"):
        check_profile(rows, TAXA, bad)
    with pytest.raises(ValueError, match="runs differ"):
        check_profile(rows, TAXA, good[1:])
    with pytest.raises(ValueError, match="without a feature_taxon row"):
        check_profile(rows + [(good[0].run_acc, "Bacteria;X;__;__;__;__;__", 0)], TAXA, good)


def test_report_outputs():
    spec, claims, runs, rows = _study()
    out, families = analyse(spec, claims, runs, rows, TAXA)
    header = {"bioproject": "PRJNA0000003", "pmcid": "PMC0000001", "signed": False, "reviewed_by": None,
              "reviewed_on": None, "profile_key": "pgmd|r|PRJNA0000003|taxon_collapsed|species",
              "delta_uri": "s3a://lake/silver/profile_abundance", "delta_version": 7, "taxon_version": 9,
              "map_path": "conf/match/x.yml", "map_sha256": "0" * 64, "min_prevalence": 3, "alpha": 0.05,
              "treatment": "antibiotics", "reference": "control"}
    md = markdown(out, families, header, {CAE: "caecum", FEC: "feces"}, [("caecum", "control", "ok", 7)],
                  {"g-cae-prevotella": "brackets dropped"})
    assert "- `g-cae-prevotella`: brackets dropped" in md
    assert "**DRAFT**" in md and "VERSION AS OF **7**" in md and "**reproduced**" in md
    assert "— no such genus in the profile" in md and "*(also)*" in md
    t = tsv_rows(out)
    assert set(t[0]) == set(fvp.TSV_COLUMNS) and len(t) == len(out) == 3 * 4
