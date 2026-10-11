"""Experimental design specs (conf/designs/<BioProject>.yml): parse, then plan against the data.

Pure: no database. The plan says which levels exist, which level every subject gets for every
factor, and what doesn't add up. Errors stop the load:
  - a subject with no value, or several values, for a factor's source attribute;
  - a value the spec doesn't declare (when the spec lists levels);
  - a nested factor whose parent is unknown or comes later in the list.
Unused declared levels are warnings.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

DesignType = Literal["completely_randomized", "randomized_block", "factorial", "crossover",
                     "repeated_measures", "observational"]
Unit = Literal["animal", "pen", "litter", "room", "farm"]
Kind = Literal["treatment", "blocking", "time", "nuisance"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")   # a typo in the YAML is an error, not a silent default


class LevelSpec(_Strict):
    control: bool = False
    description: str | None = None


class SourceSpec(_Strict):
    biosample_attr: str          # the assertion field as stored, e.g. 'treatment', 'rep', 'block'


class FactorSpec(_Strict):
    name: str
    kind: Kind
    description: str | None = None
    source: SourceSpec
    nested_in: str | None = None
    levels: dict[str, LevelSpec] | None = None   # None: the levels are whatever values the data has


class ExperimentSpec(_Strict):
    local_id: str = "main"
    title: str | None = None
    design_type: DesignType
    experimental_unit: Unit


class DesignSpec(_Strict):
    bioproject: str
    subject_attr: str
    experiment: ExperimentSpec
    factors: list[FactorSpec]


def load_spec(path: Path) -> DesignSpec:
    return DesignSpec.model_validate(yaml.safe_load(path.read_text()))


LevelKey = tuple[str | None, str]     # (parent label or None, label)


@dataclass
class DesignPlan:
    levels: dict[str, dict[LevelKey, LevelSpec]] = field(default_factory=dict)   # factor → level → spec
    assignments: dict[str, dict[str, LevelKey]] = field(default_factory=dict)   # subject → factor → level
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def plan_design(spec: DesignSpec, subject_values: dict[str, dict[str, set[str]]]) -> DesignPlan:
    """subject_values: subject local_id → assertion field → the distinct values on its samples."""
    plan = DesignPlan()
    seen: list[str] = []
    for f in spec.factors:
        if f.name in seen:
            plan.errors.append(f"factor {f.name!r} is declared twice")
        if f.nested_in and f.nested_in not in seen:
            plan.errors.append(f"factor {f.name!r} is nested in {f.nested_in!r}, which isn't declared before it")
        seen.append(f.name)
    if plan.errors:
        return plan

    by_name = {f.name: f for f in spec.factors}
    for f in spec.factors:
        levels: dict[LevelKey, LevelSpec] = {}
        used: set[str] = set()
        for subject, fields in sorted(subject_values.items()):
            values = fields.get(f.source.biosample_attr, set())
            if len(values) != 1:
                plan.errors.append(f"subject {subject}: factor {f.name!r} needs one value of "
                                   f"{f.source.biosample_attr!r}, found {sorted(values) or 'none'}")
                continue
            label = next(iter(values))
            if f.levels is not None and label not in f.levels:
                plan.errors.append(f"subject {subject}: {f.name!r} value {label!r} isn't a declared level "
                                   f"({sorted(f.levels)})")
                continue
            parent = None
            if f.nested_in:
                parent_key = plan.assignments.get(subject, {}).get(f.nested_in)
                if parent_key is None:
                    continue                      # its parent already failed and was reported
                parent = parent_key[1]
            key = (parent, label)
            levels.setdefault(key, f.levels[label] if f.levels else LevelSpec())
            plan.assignments.setdefault(subject, {})[f.name] = key
            used.add(label)
        if f.levels:
            unused = sorted(set(f.levels) - used)
            if unused:
                plan.warnings.append(f"factor {f.name!r}: declared levels never used: {unused}")
        plan.levels[f.name] = levels
        if f.nested_in and by_name[f.nested_in].nested_in:
            plan.warnings.append(f"factor {f.name!r}: nesting deeper than one level is stored but untested")
    return plan


def balance(spec: DesignSpec, plan: DesignPlan) -> list[str]:
    """For every blocking factor: how many subjects per treatment level per block (an RCBD has 1 each)."""
    lines = []
    treatments = [f.name for f in spec.factors if f.kind == "treatment"]
    for b in (f for f in spec.factors if f.kind == "blocking"):
        for t in treatments:
            cells: dict[LevelKey, Counter] = defaultdict(Counter)
            for subject, a in plan.assignments.items():
                if b.name in a and t in a:
                    cells[a[b.name]][a[t][1]] += 1
            t_levels = sorted({k[1] for k in plan.levels.get(t, {})})
            sizes = Counter(tuple(c[x] for x in t_levels) for c in cells.values())
            shape = ", ".join(f"{n}× {dict(zip(t_levels, s))}" for s, n in sizes.most_common())
            lines.append(f"{t} within {b.name}: {len(cells)} {b.name} levels → {shape}")
    return lines
