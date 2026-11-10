# Paper vs profile: PRJNA430317 (PMC7568366)

- profile `pgmd|figshare:10.6084/m9.figshare.25911745.v1|PRJNA430317|taxon_collapsed|species`
- profile_abundance `s3a://lake/silver/profile_abundance` VERSION AS OF **49**; feature_taxon VERSION AS OF **157**
- claim map `conf/match/PRJNA430317.yml` sha256 `9cdf1f089966…`, signed by Rodrigo Fontanella on 2026-11-10
- Kruskal-Wallis (two groups) on unrarefied relative abundance; BH per site, rank and scenario over every unit present in >= 3 pigs (both diets together); verdict at q < 0.05
- unit = one pig (one run per pig per site); diet from the design; PGMD direction = sign of mean-rank difference (treatment - reference)

## Runs per site, diet and depth

| site | diet | depth | runs |
|---|---|---|---|
| caecum / 67 days | antibiotics | low | 3 |
| caecum / 67 days | antibiotics | ok | 6 |
| caecum / 67 days | control | low | 2 |
| caecum / 67 days | control | ok | 7 |
| colon / 67 days | antibiotics | low | 3 |
| colon / 67 days | antibiotics | ok | 6 |
| colon / 67 days | control | low | 5 |
| colon / 67 days | control | ok | 4 |
| feces / 28 days | antibiotics | fail | 1 |
| feces / 28 days | antibiotics | low | 6 |
| feces / 28 days | antibiotics | ok | 2 |
| feces / 28 days | control | low | 9 |

## Results (scenario `all`: every run; depth is a flag, not a filter)

| claim | site | paper | PGMD taxon | present T/R | median T vs R | PGMD dir | p | q | verdict |
|---|---|---|---|---|---|---|---|---|---|
| b-feces-bacteroidetes-null | feces / 28 days | Bacteroidetes no_difference | Bacteria;Bacteroidetes | 9/9 vs 9/9 | 29.18% vs 20.23% | increase | 0.0576 | 0.346 | **consistent_null** |
| b-feces-firmicutes-null | feces / 28 days | Firmicutes no_difference | Bacteria;Firmicutes | 9/9 vs 9/9 | 23.15% vs 22.71% | increase | 0.757 | 0.825 | **consistent_null** |
| b-feces-proteobacteria-null | feces / 28 days | Proteobacteria no_difference | Bacteria;Proteobacteria | 8/9 vs 4/9 | 1.08% vs 0.00% | increase | 0.322 | 0.682 | **consistent_null** |
| b-feces-spirochaetes-null | feces / 28 days | Spirochaetes no_difference | Bacteria;Spirochaetes | 3/9 vs 2/9 | 0.00% vs 0.00% | increase | 0.402 | 0.682 | **consistent_null** |
| g-cae-asteroleplasma | caecum / 67 days | Asteroleplasma decrease (Q < 0.05) | Erysipelotrichaceae;Asteroleplasma | 0/9 vs 6/9 | 0.00% vs 1.70% | decrease | 0.00451 | 0.108 | **direction_only** |
| g-cae-campylobacter | caecum / 67 days | Campylobacter decrease (Q < 0.05) | Campylobacteraceae;Campylobacter | 0/9 vs 4/9 | 0.00% vs 0.00% | decrease | 0.0291 | 0.175 | **direction_only** |
| g-cae-dialister | caecum / 67 days | Dialister decrease (Q < 0.05) | Veillonellaceae;Dialister | 0/9 vs 4/9 | 0.00% vs 0.00% | decrease | 0.0291 | 0.175 | **direction_only** |
| g-cae-paraprevotella | caecum / 67 days | Paraprevotella increase (Q < 0.05) | — no genus Paraprevotella in the profile (only the family Paraprevotellaceae, which is not a stand-in) | | | | | | **not_testable** |
| g-cae-phascolarctobacterium | caecum / 67 days | Phascolarctobacterium increase (Q < 0.05) | Veillonellaceae;Phascolarctobacterium | 9/9 vs 7/9 | 2.47% vs 1.04% | increase | 0.2 | 0.429 | **direction_only** |
| g-cae-prevotella | caecum / 67 days | Prevotella decrease (Q < 0.05) | Prevotellaceae;Prevotella | 9/9 vs 9/9 | 16.21% vs 19.86% | decrease | 0.453 | 0.639 | **direction_only** |
| g-cae-prevotella | caecum / 67 days | Prevotella decrease (Q < 0.05) | Paraprevotellaceae;Prevotella *(also)* | 9/9 vs 9/9 | 6.60% vs 5.34% | increase | 0.453 | 0.639 | **(no verdict)** |
| g-col-acidaminococcus | colon / 67 days | Acidaminococcus decrease (Q < 0.05) | Veillonellaceae;Acidaminococcus | 1/9 vs 0/9 | 0.00% vs 0.00% |  |  |  | **not_testable** (below prevalence) |
| g-col-asteroleplasma | colon / 67 days | Asteroleplasma decrease (Q < 0.05) | Erysipelotrichaceae;Asteroleplasma | 0/9 vs 7/9 | 0.00% vs 0.34% | decrease | 0.00156 | 0.0452 | **reproduced** |
| g-col-blautia | colon / 67 days | Blautia increase (Q < 0.05) | Lachnospiraceae;Blautia | 3/9 vs 0/9 | 0.00% vs 0.00% | increase | 0.0665 | 0.214 | **direction_only** |
| g-col-butyricicoccus | colon / 67 days | Butyricicoccus increase (Q < 0.05) | Ruminococcaceae;Butyricicoccus | 1/9 vs 1/9 | 0.00% vs 0.00% |  |  |  | **not_testable** (below prevalence) |
| g-col-coprococcus | colon / 67 days | Coprococcus increase (Q < 0.05) | Lachnospiraceae;Coprococcus | 4/9 vs 4/9 | 0.00% vs 0.00% | decrease | 0.771 | 0.828 | **opposite_ns** |
| g-col-dialister | colon / 67 days | Dialister decrease (Q < 0.05) | Veillonellaceae;Dialister | 0/9 vs 1/9 | 0.00% vs 0.00% |  |  |  | **not_testable** (below prevalence) |
| g-col-mitsuokella | colon / 67 days | Mitsuokella decrease (Q < 0.05) | Veillonellaceae;Mitsuokella | 2/9 vs 4/9 | 0.00% vs 0.00% | decrease | 0.207 | 0.429 | **direction_only** |
| g-col-oscillibacter | colon / 67 days | Oscillibacter increase (Q < 0.05) | — no genus Oscillibacter in the profile (Oscillospira is a different genus, not a stand-in) | | | | | | **not_testable** |
| g-col-ruminococcus | colon / 67 days | Ruminococcus increase (Q < 0.05) | Ruminococcaceae;Ruminococcus | 7/9 vs 3/9 | 1.01% vs 0.00% | increase | 0.0376 | 0.214 | **direction_only** |
| g-col-ruminococcus | colon / 67 days | Ruminococcus increase (Q < 0.05) | Lachnospiraceae;Ruminococcus *(also)* | 1/9 vs 0/9 | 0.00% vs 0.00% |  |  |  | **not_testable** (below prevalence) |
| g-col-selenomonas | colon / 67 days | Selenomonas decrease (Q < 0.05) | Veillonellaceae;Selenomonas | 4/9 vs 5/9 | 0.00% vs 1.66% | decrease | 0.171 | 0.382 | **direction_only** |
| p-cae-bacteroidetes-trend | caecum / 67 days | Bacteroidetes no_difference (p = 0.056) | Bacteria;Bacteroidetes | 9/9 vs 9/9 | 28.62% vs 26.54% | increase | 0.965 | 0.965 | **consistent_null** |
| p-cae-firmicutes | caecum / 67 days | Firmicutes increase (p < 0.05) | Bacteria;Firmicutes | 9/9 vs 9/9 | 23.38% vs 21.25% | increase | 0.354 | 0.59 | **direction_only** |
| p-cae-tenericutes | caecum / 67 days | Tenericutes decrease (p < 0.05) | — phylum not comparable: Greengenes 13_8 puts Asteroleplasma in Erysipelotrichaceae (Firmicutes), other references in Anaeroplasmataceae (Tenericutes); the paper's reference (Supplementary S1.3) is not in the JATS | | | | | | **not_testable** |
| p-col-bacteroidetes-trend | colon / 67 days | Bacteroidetes no_difference (p = 0.076) | Bacteria;Bacteroidetes | 9/9 vs 9/9 | 16.61% vs 22.71% | decrease | 0.038 | 0.158 | **consistent_null** |
| p-col-firmicutes-trend | colon / 67 days | Firmicutes no_difference (p = 0.069) | Bacteria;Firmicutes | 9/9 vs 9/9 | 31.62% vs 29.66% | increase | 0.27 | 0.485 | **consistent_null** |
| p-col-tenericutes-trend | colon / 67 days | Tenericutes no_difference (p = 0.069) | — phylum not comparable: Greengenes 13_8 puts Asteroleplasma in Erysipelotrichaceae (Firmicutes), other references in Anaeroplasmataceae (Tenericutes); the paper's reference (Supplementary S1.3) is not in the JATS | | | | | | **not_testable** |

Present T/R = pigs with count > 0, treatment / reference. T = antibiotics, R = control.

Map notes:

- `g-cae-prevotella`: Greengenes' [Prevotella] in [Paraprevotellaceae]; PGMD drops the brackets
- `g-col-ruminococcus`: Greengenes' [Ruminococcus] in Lachnospiraceae (R. gnavus / torques group); PGMD drops the brackets
- `p-cae-firmicutes`: PGMD's Firmicutes includes Asteroleplasma (Greengenes 13_8: Erysipelotrichaceae), which the paper's reference may count as Tenericutes: not strictly like-for-like
- `p-col-firmicutes-trend`: PGMD's Firmicutes includes Asteroleplasma (Greengenes 13_8: Erysipelotrichaceae), which the paper's reference may count as Tenericutes: not strictly like-for-like

## Depth sensitivity

| claim | role | all | no_fail | ok_only | q (ok_only) |
|---|---|---|---|---|---|
| b-feces-bacteroidetes-null | verdict | consistent_null | consistent_null | n/a |  |
| b-feces-firmicutes-null | verdict | consistent_null | consistent_null | n/a |  |
| b-feces-proteobacteria-null | verdict | consistent_null | consistent_null | n/a |  |
| b-feces-spirochaetes-null | verdict | consistent_null | consistent_null | n/a |  |
| g-cae-asteroleplasma | verdict | direction_only | = all | direction_only | 0.27 |
| g-cae-campylobacter | verdict | direction_only | = all | direction_only | 0.27 |
| g-cae-dialister | verdict | direction_only | = all | direction_only | 0.27 |
| g-cae-paraprevotella | verdict | not_testable | not_testable | not_testable |  |
| g-cae-phascolarctobacterium | verdict | direction_only | = all | direction_only | 0.663 |
| g-cae-prevotella | verdict | direction_only | = all | direction_only | 0.728 |
| g-cae-prevotella | also | (no verdict) | = all | (no verdict) | 0.507 |
| g-col-acidaminococcus | verdict | not_testable | = all | not_testable |  |
| g-col-asteroleplasma | verdict | reproduced | = all | direction_only | 0.107 |
| g-col-blautia | verdict | direction_only | = all | direction_only | 0.516 |
| g-col-butyricicoccus | verdict | not_testable | = all | not_testable |  |
| g-col-coprococcus | verdict | opposite_ns | = all | opposite_ns | 0.516 |
| g-col-dialister | verdict | not_testable | = all | not_testable |  |
| g-col-mitsuokella | verdict | direction_only | = all | direction_only | 0.613 |
| g-col-oscillibacter | verdict | not_testable | not_testable | not_testable |  |
| g-col-ruminococcus | verdict | direction_only | = all | direction_only | 0.613 |
| g-col-ruminococcus | also | not_testable | = all | not_testable |  |
| g-col-selenomonas | verdict | direction_only | = all | direction_only | 0.613 |
| p-cae-bacteroidetes-trend | verdict | consistent_null | = all | consistent_null | 0.886 |
| p-cae-firmicutes | verdict | direction_only | = all | direction_only | 0.886 |
| p-cae-tenericutes | verdict | not_testable | not_testable | not_testable |  |
| p-col-bacteroidetes-trend | verdict | consistent_null | = all | consistent_null | 0.672 |
| p-col-firmicutes-trend | verdict | consistent_null | = all | consistent_null | 0.672 |
| p-col-tenericutes-trend | verdict | not_testable | not_testable | not_testable |  |

## Test families

| site | rank | scenario | pigs T/R | tested | below prevalence |
|---|---|---|---|---|---|
| caecum / 67 days | genus | all | 9/9 | 24 | 12 |
| caecum / 67 days | genus | ok_only | 6/7 | 23 | 13 |
| caecum / 67 days | phylum | all | 9/9 | 5 | 4 |
| caecum / 67 days | phylum | ok_only | 6/7 | 5 | 3 |
| colon / 67 days | genus | all | 9/9 | 29 | 15 |
| colon / 67 days | genus | ok_only | 6/4 | 27 | 15 |
| colon / 67 days | phylum | all | 9/9 | 9 | 4 |
| colon / 67 days | phylum | ok_only | 6/4 | 9 | 4 |
| feces / 28 days | phylum | all | 9/9 | 6 | 3 |
| feces / 28 days | phylum | no_fail | 8/9 | 6 | 3 |

## Summary (scenario `all`, verdict rows)

consistent_null 7, direction_only 10, not_testable 7, opposite_ns 1, reproduced 1

## What this can't tell

The paper and PGMD start from the same reads. Agreement says the claim survives reprocessing (Greengenes 13_8 instead of the paper's reference, no rarefaction, taxa collapsed to species), not that it is biologically true. Disagreement may be the pipeline, not the biology. A `consistent_null` at nine pigs per diet is weak evidence. Unrarefied data: a shallower group detects fewer rare taxa, so presence-based differences in rare genera can follow depth (compare the depth table).
