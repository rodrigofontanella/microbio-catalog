"""PGMD abundance for one study into Delta, then its profile into Postgres (handoff Step 6, design doc §5.9, §6).

    python -m app.lake.pgmd_profile --bioproject PRJNA667518 --dry-run
    python -m app.lake.pgmd_profile --bioproject PRJNA667518

Order, and why:
1. **Guards.** The study and its runs must be in the catalog. Microbial.csv in COLD_URI must match
   pgmd-v1.SHA256SUMS (sha256 streamed in the JVM, ~293 MB, no copy on Leeloo).
2. **Bronze** `LAKE_URI/bronze/pgmd_microbial_raw`: the whole file, every level, all columns as strings as
   downloaded, plus _source, _source_release, _source_uri, _source_sha256, _ingest_run_id, _loaded_at. Loaded
   once per release: a release already in the table's history with the same sha256 adds 0 rows; with another
   sha256 the load stops.
3. **Silver**, the study's Species rows only, keyed by profile_key (Postgres has no profile_id yet):
   `silver/feature_taxon` (lineage split into ranks, resolved rank, PGMD's label) and
   `silver/profile_abundance` (run_acc, feature_id = normalized lineage, count, rel_abund recomputed from
   count; PGMD's rounded `abundance` stays in bronze). A profile_key already in a table's history adds 0 rows,
   after the stored rows are compared with the recomputed ones; any difference stops the load.
4. **The genus test**, before silver is written: Species counts summed to their 6-segment lineage must equal
   PGMD's own Genus rows, (run, genus) by (run, genus). Any difference stops the load.
5. **Postgres after the Delta commit**, in one transaction: core.profile (delta_version = the
   profile_abundance commit that holds this profile_key), core.profile_run (n_reads, n_features), and the
   `inferred` reference assertions (Greengenes 13_8) on the profile. A stored profile that differs stops.

Tables are created append-only with 10-year log and file retention (decided 2026-10-13), so
`VERSION AS OF core.profile.delta_version` stays readable; no OPTIMIZE/VACUUM is scheduled.

Spark shape (local[2], 4 shuffle partitions): the CSV is read in ~3 splits of 128 MB and written to bronze with
no shuffle. Silver reads bronze with column pruning (~2M rows scanned, ~22k kept); rel_abund is a window per run
(one shuffle, 4 partitions, coalesced by AQE); each silver load is coalesced to one file. The genus test is two
small aggregations and a full outer join (~18k keys). Nothing here should spill; if it does, the scratch dir is
SPARK_LOCAL_DIR.

Delta on S3: one writer per table at a time. Run one instance of this job at a time.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.config import Settings, settings
from app.lake import lineage as lin

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

SOURCE = "pgmd"
CSV_NAME = "Microbial.csv"
SUMS_NAME = "pgmd-v1.SHA256SUMS"
FEATURE_TYPE = "taxon_collapsed"
TAXON_RANK = "species"
LEVEL = "Species"
ABUNDANCE_KIND = "counts"
CSV_COLUMNS = ("id", "microbial_name", "specific_taxonomy", "level", "count", "abundance", "Runs", "BioProject")
TABLE_PROPERTIES = {
    "delta.appendOnly": "true",
    "delta.logRetentionDuration": "interval 3650 days",
    "delta.deletedFileRetentionDuration": "interval 3650 days",
}
REFERENCE_METHOD = "pgmd_profile:reference_from_lineage_shape"


class Stop(Exception):
    """A guard failed: nothing more is written, and the reason is recorded on the ingest run."""


def profile_key(source_release: str, bioproject: str) -> str:
    return "|".join((SOURCE, source_release, bioproject, FEATURE_TYPE, TAXON_RANK))


@dataclass(frozen=True)
class Paths:
    csv: str
    sums: str
    bronze: str
    feature_taxon: str
    profile_abundance: str

    @classmethod
    def from_settings(cls, s: Settings) -> "Paths":
        raw = f"{s.cold_uri.rstrip('/')}/{s.pgmd_raw_prefix.strip('/')}"
        lake = s.lake_uri.rstrip("/")
        return cls(csv=f"{raw}/{CSV_NAME}", sums=f"{raw}/{SUMS_NAME}", bronze=f"{lake}/bronze/pgmd_microbial_raw",
                   feature_taxon=f"{lake}/silver/feature_taxon", profile_abundance=f"{lake}/silver/profile_abundance")


# ── schemas (explicit, always) ─────────────────────────────────────────────────────────────────

def csv_schema():
    from pyspark.sql.types import StringType, StructField, StructType
    return StructType([StructField(c, StringType(), True) for c in CSV_COLUMNS])


def bronze_schema():
    from pyspark.sql.types import LongType, StringType, StructField, StructType, TimestampType
    return StructType(csv_schema().fields + [
        StructField("_source", StringType(), False),
        StructField("_source_release", StringType(), False),
        StructField("_source_uri", StringType(), False),
        StructField("_source_sha256", StringType(), False),
        StructField("_ingest_run_id", LongType(), True),        # NULL only in a dry run (never written)
        StructField("_loaded_at", TimestampType(), False),
    ])


def abundance_schema():
    from pyspark.sql.types import DoubleType, LongType, StringType, StructField, StructType, TimestampType
    return StructType([
        StructField("profile_key", StringType(), False),
        StructField("run_acc", StringType(), False),
        StructField("feature_id", StringType(), False),
        StructField("count", LongType(), False),
        StructField("rel_abund", DoubleType(), False),
        StructField("_source_sha256", StringType(), False),
        StructField("_bronze_version", LongType(), True),
        StructField("_ingest_run_id", LongType(), True),
        StructField("_loaded_at", TimestampType(), False),
    ])


def taxon_schema():
    from pyspark.sql.types import (BooleanType, IntegerType, LongType, StringType, StructField, StructType,
                                   TimestampType)
    return StructType(
        [StructField("profile_key", StringType(), False), StructField("feature_id", StringType(), False)]
        + [StructField(r, StringType(), True) for r in lin.RANKS]
        + [StructField("resolved_rank", StringType(), True),
           StructField("resolved_depth", IntegerType(), False),
           StructField("has_gap", BooleanType(), False),
           StructField("external_label", StringType(), True),     # PGMD's microbial_name, a label, never a key
           StructField("_ingest_run_id", LongType(), True),
           StructField("_loaded_at", TimestampType(), False)])


# ── transforms (DataFrame in, DataFrame out: tested on a fixture) ──────────────────────────────

def read_csv(spark: "SparkSession", uri: str) -> "DataFrame":
    """As downloaded: every column a string. enforceSchema=false makes Spark check the header against the schema."""
    return (spark.read.schema(csv_schema()).option("header", "true").option("enforceSchema", "false")
            .option("mode", "FAILFAST").option("encoding", "UTF-8").csv(uri))


def check_header(spark: "SparkSession", uri: str) -> list[str]:
    """The CSV's header, checked up front: Spark only validates the columns a query reads (count() reads none)."""
    first = spark.read.text(uri).first()
    header = [c.strip().strip('"') for c in (first.value if first else "").lstrip("\ufeff").split(",")]
    if header != list(CSV_COLUMNS):
        raise Stop(f"{uri}: header {header} != expected {list(CSV_COLUMNS)}")
    return header


def stamp_bronze(raw: "DataFrame", source_release: str, source_uri: str, sha256: str,
                 ingest_run_id: int | None, loaded_at: datetime) -> "DataFrame":
    from pyspark.sql import functions as F
    return raw.select(*CSV_COLUMNS,
                     F.lit(SOURCE).alias("_source"), F.lit(source_release).alias("_source_release"),
                     F.lit(source_uri).alias("_source_uri"), F.lit(sha256).alias("_source_sha256"),
                     F.lit(ingest_run_id).cast("long").alias("_ingest_run_id"),
                     F.lit(loaded_at).cast("timestamp").alias("_loaded_at"))


def _segments(col: str):
    from pyspark.sql import functions as F
    return F.transform(F.split(F.col(col), ";"), lambda x: F.trim(x))


def normalized_lineage(col: str = "specific_taxonomy"):
    from pyspark.sql import functions as F
    return F.array_join(_segments(col), ";")


def genus_lineage(col: str = "specific_taxonomy"):
    from pyspark.sql import functions as F
    return F.array_join(F.slice(_segments(col), 1, 6), ";")


def level_counts(study_rows: "DataFrame") -> dict[str, int]:
    return {r["level"]: r["n"] for r in study_rows.groupBy("level").count().withColumnRenamed("count", "n").collect()}


def species_problems(species: "DataFrame") -> dict[str, Any]:
    """Shape checks on the study's Species rows. Any non-zero count except zero_counts stops the load."""
    from pyspark.sql import functions as F
    seg_n = F.size(F.split(F.col("specific_taxonomy"), ";"))
    agg = species.agg(
        F.count(F.lit(1)).alias("rows"),
        F.sum(F.when(F.col("Runs").isNull() | F.col("specific_taxonomy").isNull(), 1).otherwise(0)).alias("null_keys"),
        F.sum(F.when(~F.coalesce(F.col("count"), F.lit("")).rlike(r"^[0-9]+$"), 1).otherwise(0)).alias("non_integer_counts"),
        F.sum(F.when(seg_n != lin.LEVEL_SEGMENTS[LEVEL], 1).otherwise(0)).alias("wrong_segment_count"),
        F.sum(F.when(F.col("count") == "0", 1).otherwise(0)).alias("zero_counts"),
    ).first().asDict()
    keyed = species.select(F.trim("Runs").alias("run_acc"), normalized_lineage().alias("feature_id"))
    agg["duplicate_run_features"] = keyed.groupBy("run_acc", "feature_id").count().filter("count > 1").count()
    labels = species.select(normalized_lineage().alias("feature_id"), "microbial_name").distinct()
    agg["features_with_two_labels"] = labels.groupBy("feature_id").count().filter("count > 1").count()
    agg["lineages_not_normalized"] = species.filter(normalized_lineage() != F.col("specific_taxonomy")).count()
    return agg


def build_abundance(species: "DataFrame", key: str, sha256: str, bronze_version: int | None,
                    ingest_run_id: int | None, loaded_at: datetime) -> "DataFrame":
    from pyspark.sql import Window
    from pyspark.sql import functions as F
    per_run = Window.partitionBy("run_acc")
    df = (species.select(F.trim("Runs").alias("run_acc"), normalized_lineage().alias("feature_id"),
                         F.col("count").cast("long").alias("count"))
          .withColumn("rel_abund", F.col("count") / F.sum("count").over(per_run)))
    return df.select(F.lit(key).alias("profile_key"), "run_acc", "feature_id", "count",
                     F.col("rel_abund").cast("double").alias("rel_abund"),
                     F.lit(sha256).alias("_source_sha256"),
                     F.lit(bronze_version).cast("long").alias("_bronze_version"),
                     F.lit(ingest_run_id).cast("long").alias("_ingest_run_id"),
                     F.lit(loaded_at).cast("timestamp").alias("_loaded_at"))


def taxon_rows(species: "DataFrame", key: str, ingest_run_id: int | None, loaded_at: datetime) -> list[tuple]:
    """Distinct lineages of the study (hundreds to a few thousand), parsed on the driver by lineage.taxon_row."""
    pairs = species.select(normalized_lineage().alias("feature_id"), "microbial_name").distinct().collect()
    rows = []
    for r in sorted(pairs, key=lambda p: p["feature_id"]):
        t = lin.taxon_row(r["feature_id"], lin.LEVEL_SEGMENTS[LEVEL])
        rows.append((key, r["feature_id"], *[t[x] for x in lin.RANKS], t["resolved_rank"], t["resolved_depth"],
                     t["has_gap"], r["microbial_name"], ingest_run_id, loaded_at))
    return rows


def per_run_stats(abundance: "DataFrame") -> list[dict[str, Any]]:
    from pyspark.sql import functions as F
    return [r.asDict() for r in abundance.groupBy("run_acc").agg(
        F.sum("count").alias("n_reads"),
        F.sum(F.when(F.col("count") > 0, 1).otherwise(0)).cast("int").alias("n_features"),
        F.count(F.lit(1)).alias("n_rows")).orderBy("run_acc").collect()]


def genus_test(abundance: "DataFrame", study_rows: "DataFrame") -> dict[str, Any]:
    """Species counts rolled up to the 6-segment lineage vs PGMD's own Genus rows, per (run, genus)."""
    from pyspark.sql import functions as F
    sp = (abundance.select("run_acc", genus_lineage("feature_id").alias("genus"), "count")
          .groupBy("run_acc", "genus").agg(F.sum("count").alias("species_sum")))
    ge = (study_rows.filter(F.col("level") == "Genus")
          .select(F.trim("Runs").alias("run_acc"), genus_lineage().alias("genus"), F.col("count").cast("long").alias("c"))
          .groupBy("run_acc", "genus").agg(F.sum("c").alias("genus_row")))
    j = sp.join(ge, ["run_acc", "genus"], "full_outer")
    verdict = (F.when(F.col("genus_row").isNull(), "only_in_species")
               .when(F.col("species_sum").isNull(), "only_in_genus")
               .when(F.col("species_sum") == F.col("genus_row"), "equal")
               .when(F.col("species_sum") > F.col("genus_row"), "species_larger")
               .otherwise("species_smaller"))
    j = j.withColumn("verdict", verdict)
    counts = {r["verdict"]: r["n"] for r in j.groupBy("verdict").agg(F.count(F.lit(1)).alias("n")).collect()}
    runs = j.groupBy("run_acc").agg(F.min(F.when(F.col("verdict") == "equal", 1).otherwise(0)).alias("ok"))
    examples = [r.asDict() for r in j.filter(F.col("verdict") != "equal").orderBy("run_acc", "genus").limit(5).collect()]
    out = {"keys": sum(counts.values()), "equal": counts.get("equal", 0), "verdicts": counts,
           "runs": runs.count(), "runs_all_equal": runs.filter("ok = 1").count(), "examples": examples}
    out["passed"] = out["keys"] > 0 and out["equal"] == out["keys"] and out["runs"] == out["runs_all_equal"]
    return out


def reference_evidence(species: "DataFrame") -> dict[str, Any]:
    """Lineage-shape evidence for 'Greengenes 13_8, brackets stripped' (design doc §2.1.1), measured per profile."""
    from pyspark.sql import functions as F
    feats = species.select(normalized_lineage().alias("f")).distinct()
    r = feats.agg(
        F.count(F.lit(1)).alias("distinct_lineages"),
        F.sum(F.when(F.col("f").contains("_group") | F.col("f").contains("UCG-"), 1).otherwise(0)).alias("silva_style_names"),
        F.sum(F.when(F.col("f").contains("[") | F.col("f").contains("]"), 1).otherwise(0)).alias("bracketed_names"),
        F.sum(F.when(F.col("f").contains(";Ruminococcaceae;Oscillospira"), 1).otherwise(0)).alias("gg_ruminococcaceae_oscillospira"),
        F.sum(F.when(F.col("f").endswith(";p-1630-c5"), 1).otherwise(0)).alias("gg_placeholder_p_1630_c5"),
    ).first().asDict()
    return {k: int(v or 0) for k, v in r.items()}


# ── Spark / Delta helpers ──────────────────────────────────────────────────────────────────────

def sha256_of(spark: "SparkSession", uri: str) -> str:
    """Stream the object through the JVM (commons-codec ships with Spark): no local copy, no Python transfer."""
    jvm = spark.sparkContext._jvm
    path = jvm.org.apache.hadoop.fs.Path(uri)
    fs = path.getFileSystem(spark.sparkContext._jsc.hadoopConfiguration())
    stream = fs.open(path)
    try:
        return str(jvm.org.apache.commons.codec.digest.DigestUtils.sha256Hex(stream))
    finally:
        stream.close()


def expected_sha256(spark: "SparkSession", sums_uri: str, name: str) -> str:
    for line in (r.value for r in spark.read.text(sums_uri).collect()):
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip().lstrip("*") == name:
            return parts[0].lower()
    raise Stop(f"{name} is not listed in {sums_uri}")


def ensure_table(spark: "SparkSession", uri: str, schema) -> bool:
    """Create the path-based table with its properties if it doesn't exist. Returns True if created."""
    from delta.tables import DeltaTable
    if DeltaTable.isDeltaTable(spark, uri):
        return False
    b = DeltaTable.createIfNotExists(spark).location(uri).addColumns(schema)
    for k, v in TABLE_PROPERTIES.items():
        b = b.property(k, v)
    b.execute()
    return True


def commits_with(spark: "SparkSession", uri: str, **match: str) -> list[tuple[int, dict]]:
    """(version, userMetadata) of WRITE commits whose userMetadata JSON contains all of `match`."""
    from delta.tables import DeltaTable
    if not DeltaTable.isDeltaTable(spark, uri):
        return []
    out = []
    for r in DeltaTable.forPath(spark, uri).history().select("version", "operation", "userMetadata").collect():
        if r["operation"] != "WRITE" or not r["userMetadata"]:
            continue
        try:
            md = json.loads(r["userMetadata"])
        except ValueError:
            continue
        if all(md.get(k) == v for k, v in match.items()):
            out.append((int(r["version"]), md))
    return sorted(out)


def one_commit(spark: "SparkSession", uri: str, **match: str) -> tuple[int, dict] | None:
    found = commits_with(spark, uri, **match)
    if len(found) > 1:
        raise Stop(f"{uri}: {len(found)} commits match {match} (versions {[v for v, _ in found]}); expected at most 1")
    return found[0] if found else None


def append(df: "DataFrame", uri: str, metadata: dict, files: int | None = None) -> None:
    if files:
        df = df.coalesce(files)
    df.write.format("delta").mode("append").option("userMetadata", json.dumps(metadata, sort_keys=True)).save(uri)


def same_rows(a: "DataFrame", b: "DataFrame", cols: list[str]) -> tuple[int, int]:
    """Rows only in a, rows only in b (multiset difference on cols)."""
    a, b = a.select(*cols), b.select(*cols)
    return a.exceptAll(b).count(), b.exceptAll(a).count()


# ── Postgres (async SQLAlchemy, one engine per phase: asyncpg connections belong to one event loop) ─────

def _engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    return create_async_engine(settings.database_url, poolclass=NullPool)


async def _preflight(bioproject: str, key: str) -> dict[str, Any]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models import Profile, Run, Study
    eng = _engine()
    try:
        async with AsyncSession(eng) as db:
            study_id = (await db.execute(select(Study.study_id).where(Study.bioproject_acc == bioproject))).scalar_one_or_none()
            runs = set() if study_id is None else set(
                (await db.execute(select(Run.run_acc).where(Run.study_id == study_id))).scalars())
            prof = (await db.execute(select(Profile.profile_id).where(Profile.profile_key == key))).scalar_one_or_none()
        return {"study_id": study_id, "runs": runs, "profile_id": prof}
    finally:
        await eng.dispose()


async def _start_ingest_run(source_release: str, raw_uri: str, params: dict) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models import IngestRun
    eng = _engine()
    try:
        async with AsyncSession(eng) as db:
            ir = IngestRun(source=SOURCE, source_release=source_release, params=params, raw_uri=raw_uri)
            db.add(ir)
            await db.flush()
            irid = ir.ingest_run_id          # read before commit: commit expires the object
            await db.commit()
            return irid
    finally:
        await eng.dispose()


async def _record_failure(irid: int, params: dict, error: str) -> None:
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models import IngestRun
    eng = _engine()
    try:
        async with AsyncSession(eng) as db:     # finished_at stays NULL: a failed run is registered, not finished
            await db.execute(update(IngestRun).where(IngestRun.ingest_run_id == irid)
                             .values(params={**params, "error": error}))
            await db.commit()
    finally:
        await eng.dispose()


@dataclass
class ProfilePlan:
    key: str
    study_id: int
    source_release: str
    delta_uri: str
    delta_version: int
    source_uri: str
    source_sha256: str
    runs: list[dict[str, Any]]
    reference: dict[str, Any] | None          # evidence; None = the inference didn't hold, no assertion
    params: dict[str, Any] = field(default_factory=dict)


async def _write_profile(irid: int, plan: ProfilePlan) -> dict[str, int]:
    """One transaction: profile, its runs, its reference assertions, finished_at. Differences stop it."""
    from sqlalchemy import select, update
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models import Assertion, IngestRun, Profile, ProfileRun
    want = {"profile_key": plan.key, "source": SOURCE, "source_release": plan.source_release,
            "study_id": plan.study_id, "feature_type": FEATURE_TYPE, "taxon_rank": TAXON_RANK,
            "abundance_kind": ABUNDANCE_KIND, "delta_uri": plan.delta_uri, "delta_version": plan.delta_version,
            "source_uri": plan.source_uri, "source_sha256": plan.source_sha256}
    counts = {"profile.new": 0, "profile_run.new": 0, "profile_run.existing": 0, "assertion.new": 0,
              "assertion.existing": 0}
    eng = _engine()
    try:
        async with AsyncSession(eng) as db:
            res = await db.execute(pg_insert(Profile).values(**want, ingest_run_id=irid)
                                   .on_conflict_do_nothing(index_elements=["profile_key"]).returning(Profile.profile_id))
            new_id = res.scalar_one_or_none()
            if new_id is not None:
                counts["profile.new"] = 1
                profile_id = new_id
            else:
                stored = (await db.execute(select(Profile).where(Profile.profile_key == plan.key))).scalar_one()
                diff = {k: (getattr(stored, k), v) for k, v in want.items() if getattr(stored, k) != v}
                if diff:
                    raise Stop(f"stored core.profile {stored.profile_id} differs (stored, new): {diff}")
                profile_id = stored.profile_id

            have = {r.run_acc: (r.n_reads, r.n_features) for r in (await db.execute(
                select(ProfileRun.run_acc, ProfileRun.n_reads, ProfileRun.n_features)
                .where(ProfileRun.profile_id == profile_id))).all()}
            rows = []
            for r in plan.runs:
                if r["run_acc"] in have:
                    if have[r["run_acc"]] != (r["n_reads"], r["n_features"]):
                        raise Stop(f"stored profile_run {r['run_acc']} differs: {have[r['run_acc']]} vs "
                                   f"{(r['n_reads'], r['n_features'])}")
                    counts["profile_run.existing"] += 1
                else:
                    rows.append({"profile_id": profile_id, "run_acc": r["run_acc"], "study_id": plan.study_id,
                                 "n_reads": r["n_reads"], "n_features": r["n_features"], "ingest_run_id": irid})
            extra = set(have) - {r["run_acc"] for r in plan.runs}
            if extra:
                raise Stop(f"core.profile_run has runs the profile no longer has: {sorted(extra)[:5]}")
            if rows:
                await db.execute(pg_insert(ProfileRun).values(rows))
            counts["profile_run.new"] = len(rows)

            if plan.reference is not None:
                existing = {(r.field, r.value_text) for r in (await db.execute(
                    select(Assertion.field, Assertion.value_text)
                    .where(Assertion.profile_id == profile_id, Assertion.method == REFERENCE_METHOD))).all()}
                for fld, val in (("reference_db", "Greengenes"), ("reference_version", "13_8")):
                    if (fld, val) in existing:
                        counts["assertion.existing"] += 1
                        continue
                    db.add(Assertion(profile_id=profile_id, field=fld, value_text=val, tier="inferred",
                                     method=REFERENCE_METHOD, evidence=plan.reference, ingest_run_id=irid))
                    counts["assertion.new"] += 1

            await db.execute(update(IngestRun).where(IngestRun.ingest_run_id == irid).values(
                finished_at=datetime.now(timezone.utc),
                params={**plan.params, "profile_id": profile_id, "counts": counts}))
            await db.commit()
        counts["profile_id"] = profile_id
        return counts
    finally:
        await eng.dispose()


# ── the job ────────────────────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m app.lake.pgmd_profile", description=__doc__.splitlines()[0])
    ap.add_argument("--bioproject", required=True, help="the study whose Species rows go to silver")
    ap.add_argument("--dry-run", action="store_true",
                    help="every guard, transform and the genus test; no ingest run, no Delta or Postgres writes")
    return ap


def run(argv: list[str] | None = None) -> int:
    from pyspark.sql import functions as F
    from app.ingest.ncbi_study import code_version
    from app.ingest.pgmd_parse import SOURCE_RELEASE
    from app.lake.spark import build_spark

    args = build_parser().parse_args(argv)
    bioproject = args.bioproject.strip().upper()
    paths = Paths.from_settings(settings)
    key = profile_key(SOURCE_RELEASE, bioproject)
    loaded_at = datetime.now(timezone.utc).replace(microsecond=0)

    pre = asyncio.run(_preflight(bioproject, key))
    if pre["study_id"] is None or not pre["runs"]:
        log(f"ERROR {bioproject}: not in core.study, or no core.run rows; load the study first")
        return 1
    log(f"catalog: {bioproject} study_id {pre['study_id']}, {len(pre['runs'])} runs; "
        f"profile {'exists: ' + str(pre['profile_id']) if pre['profile_id'] else 'not yet loaded'}")

    spark = build_spark("pgmd-profile")
    params: dict[str, Any] = {"bioproject": bioproject, "profile_key": key, "source_uri": paths.csv,
                              "tables": {"bronze": paths.bronze, "feature_taxon": paths.feature_taxon,
                                         "profile_abundance": paths.profile_abundance},
                              "dry_run": args.dry_run, **code_version()}
    irid: int | None = None
    try:
        want = expected_sha256(spark, paths.sums, CSV_NAME)
        got = sha256_of(spark, paths.csv)
        log(f"sha256 {CSV_NAME}: {got} ({'matches' if got == want else 'DOES NOT MATCH'} {SUMS_NAME})")
        if got != want:
            raise Stop(f"{paths.csv} sha256 {got} != {want} in {SUMS_NAME}: the source file changed")
        params["sha256"] = got

        if not args.dry_run:
            irid = asyncio.run(_start_ingest_run(SOURCE_RELEASE, paths.csv, params))
            log(f"ingest_run {irid} started")

        # ── bronze: the whole file, once per release ──
        found = one_commit(spark, paths.bronze, source_release=SOURCE_RELEASE)
        if found and found[1].get("source_sha256") != got:
            raise Stop(f"bronze already holds {SOURCE_RELEASE} with sha256 {found[1].get('source_sha256')}, "
                       f"the file now has {got}: a changed source file is a new release, not a reload")
        if found:
            bronze_version = found[0]
            log(f"bronze: {SOURCE_RELEASE} already loaded at version {bronze_version}; 0 rows added")
            params["bronze"] = {"version": bronze_version, "added": 0}
            bronze = spark.read.format("delta").option("versionAsOf", bronze_version).load(paths.bronze)
        else:
            check_header(spark, paths.csv)
            raw = read_csv(spark, paths.csv)
            stamped = stamp_bronze(raw, SOURCE_RELEASE, paths.csv, got, irid, loaded_at)
            if args.dry_run:
                bronze_version, bronze = None, stamped
                log("bronze: not loaded yet (dry run reads the CSV directly)")
            else:
                created = ensure_table(spark, paths.bronze, bronze_schema())
                n_csv = raw.count()
                append(stamped, paths.bronze, {"source_release": SOURCE_RELEASE, "source_sha256": got,
                                               "ingest_run_id": irid, "rows": n_csv})
                bronze_version = one_commit(spark, paths.bronze, source_release=SOURCE_RELEASE)[0]
                bronze = spark.read.format("delta").option("versionAsOf", bronze_version).load(paths.bronze)
                n_stored = bronze.filter(F.col("_source_sha256") == got).count()
                log(f"bronze: {'created, ' if created else ''}{n_csv} CSV rows → {n_stored} rows at version {bronze_version}")
                if n_stored != n_csv:
                    raise Stop(f"bronze holds {n_stored} rows for this file, the CSV has {n_csv}")
                params["bronze"] = {"version": bronze_version, "added": n_stored}
        bronze = bronze.filter((F.col("_source_release") == SOURCE_RELEASE) & (F.col("_source_sha256") == got))

        study = bronze.filter(F.trim("BioProject") == bioproject).cache()
        levels = level_counts(study)
        log(f"bronze rows for {bioproject}: {sum(levels.values())}  " + "  ".join(
            f"{lv} {levels.get(lv, 0)}" for lv in lin.LEVEL_SEGMENTS))
        params["bronze_rows_for_study"] = levels
        species = study.filter(F.col("level") == LEVEL)

        # ── silver guards, before anything is written ──
        problems = species_problems(species)
        log("species checks: " + json.dumps(problems, sort_keys=True))
        params["species_checks"] = problems
        bad = {k: v for k, v in problems.items() if k not in ("rows", "zero_counts") and v}
        if not problems["rows"] or bad:
            raise Stop(f"species rows fail the shape checks: {bad or 'no rows'}")
        abundance = build_abundance(species, key, got, bronze_version, irid, loaded_at).cache()
        stats = per_run_stats(abundance)
        unknown_runs = sorted({r["run_acc"] for r in stats} - pre["runs"])
        if unknown_runs:
            raise Stop(f"{len(unknown_runs)} runs are not core.run rows of {bioproject}: {unknown_runs[:5]}")
        reads = [r["n_reads"] for r in stats]
        feats = [r["n_features"] for r in stats]
        log(f"silver: {abundance.count()} rows, {len(stats)} runs (catalog has {len(pre['runs'])}); "
            f"reads per run min {min(reads)} median {statistics.median(reads):.0f} max {max(reads)}; "
            f"features per run min {min(feats)} median {statistics.median(feats):.0f} max {max(feats)}")

        gt = genus_test(abundance, study)
        log(f"genus test: (run, genus) keys {gt['keys']}: equal {gt['equal']} {gt['verdicts']}; "
            f"runs where every genus matches: {gt['runs_all_equal']} of {gt['runs']} → {'PASS' if gt['passed'] else 'FAIL'}")
        params["genus_test"] = {k: v for k, v in gt.items() if k != "examples"}
        if not gt["passed"]:
            for e in gt["examples"]:
                log(f"  e.g. {e}")
            raise Stop("genus test failed: species do not roll up to PGMD's genus rows")

        ref = reference_evidence(species)
        ref_holds = ref["silva_style_names"] == 0 and ref["bracketed_names"] == 0
        log(f"reference evidence: {json.dumps(ref, sort_keys=True)} → "
            f"{'Greengenes 13_8 (inferred)' if ref_holds else 'NOT asserted: SILVA-style or bracketed names present'}")
        params["reference_evidence"] = ref

        taxa = spark.createDataFrame(taxon_rows(species, key, irid, loaded_at), taxon_schema())
        if args.dry_run:
            log(f"dry run: feature_taxon {taxa.count()} rows, profile_abundance {abundance.count()} rows; nothing written")
            return 0

        # ── silver writes (feature_taxon first, then the abundance commit that core.profile points at) ──
        versions = {}
        for name, uri, df, schema, cols in (
                ("feature_taxon", paths.feature_taxon, taxa, taxon_schema(), ["feature_id", *lin.RANKS, "external_label"]),
                ("profile_abundance", paths.profile_abundance, abundance, abundance_schema(), ["run_acc", "feature_id", "count"])):
            found = one_commit(spark, uri, profile_key=key)
            if found:
                if found[1].get("source_sha256") != got:
                    raise Stop(f"{name} holds {key} from sha256 {found[1].get('source_sha256')}, not {got}")
                stored = spark.read.format("delta").option("versionAsOf", found[0]).load(uri) \
                    .filter(F.col("profile_key") == key)
                only_stored, only_new = same_rows(stored, df, cols)
                if only_stored or only_new:
                    raise Stop(f"{name}: stored rows for {key} differ from the recomputed ones "
                               f"({only_stored} only stored, {only_new} only new)")
                versions[name] = found[0]
                log(f"{name}: {key} already at version {found[0]}; stored rows equal the recomputed ones; 0 rows added")
            else:
                created = ensure_table(spark, uri, schema)
                n = df.count()
                append(df, uri, {"profile_key": key, "source_sha256": got, "bronze_version": bronze_version,
                                 "ingest_run_id": irid, "rows": n}, files=1)
                versions[name] = one_commit(spark, uri, profile_key=key)[0]
                log(f"{name}: {'created, ' if created else ''}{n} rows at version {versions[name]}")
        params["delta_versions"] = versions

        # ── Postgres, after the Delta commit ──
        plan = ProfilePlan(key=key, study_id=pre["study_id"], source_release=SOURCE_RELEASE,
                           delta_uri=paths.profile_abundance, delta_version=versions["profile_abundance"],
                           source_uri=paths.csv, source_sha256=got,
                           runs=[{k: r[k] for k in ("run_acc", "n_reads", "n_features")} for r in stats],
                           reference={"basis": "lineage shape, design doc §2.1.1", **ref} if ref_holds else None,
                           params=params)
        counts = asyncio.run(_write_profile(irid, plan))
        log(f"postgres: ingest_run {irid} committed: " + json.dumps(counts, sort_keys=True))
        return 0
    except Stop as e:
        log(f"ERROR {e}")
        if irid is not None:
            asyncio.run(_record_failure(irid, params, str(e)))
            log(f"ingest_run {irid} recorded as failed (finished_at NULL)")
        return 1
    except Exception as e:      # a crash after the ingest run started is still recorded, then re-raised
        if irid is not None:
            asyncio.run(_record_failure(irid, params, f"{type(e).__name__}: {e}"[:2000]))
            log(f"ingest_run {irid} recorded as failed (finished_at NULL)")
        raise
    finally:
        spark.stop()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
