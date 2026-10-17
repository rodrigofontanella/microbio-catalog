"""One way to build the Spark session: local mode on Leeloo, Delta on the S3-compatible store on Skipper.

Everything comes from settings (.env), nothing is hardcoded:
    S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION, LAKE_URI, COLD_URI,
    SPARK_MASTER, SPARK_DRIVER_MEMORY, SPARK_SHUFFLE_PARTITIONS, SPARK_LOCAL_DIR, SPARK_PACKAGES, SPARK_JARS_EXCLUDES

Sizing: local[2], 2 GB driver, 4 shuffle partitions. The data is tens of thousands of rows per study and ~2M
for all of PGMD, so the defaults (200 shuffle partitions, all cores) would only add overhead.
Delta on S3: ONE writer per table at a time. With one driver the default S3 log store is safe; two drivers
writing the same table concurrently can lose a commit.

Classpath: pyspark's own jars are on the JVM's system classpath; spark.jars.packages jars load in a child class
loader that asks its parent first. A class present in both comes from pyspark; a class only in the package jar
comes from there, mixed with pyspark's versions of its neighbours. Hadoop must be exactly one version
(hadoop-aws 3.5.0 needs Hadoop 3.5.0), so Delta's transitive hadoop-client-* 3.4.2 is excluded
(SPARK_JARS_EXCLUDES). The other duplicates (parquet, jackson, RoaringBitmap, …) are reported by
classpath_report() and stay shadowed: decided 2026-10-17.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import Settings, settings as default_settings

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

SECRET_KEYS = ("spark.hadoop.fs.s3a.access.key", "spark.hadoop.fs.s3a.secret.key")


def spark_conf(s: Settings) -> dict[str, str]:
    """Spark config for local mode + Delta + s3a. Contains the secrets: never print it, use redacted()."""
    missing = [n for n, v in (("S3_ENDPOINT", s.s3_endpoint), ("S3_ACCESS_KEY", s.s3_access_key.get_secret_value()),
                              ("S3_SECRET_KEY", s.s3_secret_key.get_secret_value())) if not v]
    if missing:
        raise ValueError(f"missing in .env: {', '.join(missing)}")
    conf = {
        "spark.master": s.spark_master,
        "spark.driver.memory": s.spark_driver_memory,
        "spark.sql.shuffle.partitions": str(s.spark_shuffle_partitions),
        "spark.jars.packages": s.spark_packages,
        "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension",
        "spark.sql.catalog.spark_catalog": "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        "spark.sql.session.timeZone": "UTC",
        "spark.ui.showConsoleProgress": "false",
        "spark.hadoop.fs.s3a.endpoint": s.s3_endpoint,
        "spark.hadoop.fs.s3a.endpoint.region": s.s3_region,
        "spark.hadoop.fs.s3a.path.style.access": "true",
        "spark.hadoop.fs.s3a.connection.ssl.enabled": str(s.s3_endpoint.startswith("https://")).lower(),
        "spark.hadoop.fs.s3a.access.key": s.s3_access_key.get_secret_value(),
        "spark.hadoop.fs.s3a.secret.key": s.s3_secret_key.get_secret_value(),
    }
    if s.spark_local_dir:
        conf["spark.local.dir"] = s.spark_local_dir
    if s.spark_jars_excludes:
        conf["spark.jars.excludes"] = s.spark_jars_excludes
    return conf


def redacted(conf: dict[str, str]) -> dict[str, str]:
    return {k: ("***" if k in SECRET_KEYS else v) for k, v in conf.items()}


def build_spark(app_name: str, s: Settings | None = None) -> "SparkSession":
    from pyspark.sql import SparkSession

    builder = SparkSession.builder.appName(app_name)
    for k, v in spark_conf(s or default_settings).items():
        builder = builder.config(k, v)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    return spark


# ── classpath: which jars came from spark.jars.packages, and which of them pyspark already bundles ──

_JAR = re.compile(r"^(?P<artifact>.+?)-(?P<version>\d[^/]*)\.jar$")


def jar_coordinates(name: str, ivy: bool) -> tuple[str, str] | None:
    """(artifact, version) from a jar file name. Ivy names jars '<organisation>_<artifact>-<revision>.jar'
    (the organisation can lack a dot: 'commons-pool_commons-pool-1.6.jar'); pyspark's are '<artifact>-<version>.jar'."""
    base = name.rsplit("/", 1)[-1]
    if ivy:
        if "_" not in base:
            return None
        base = base.split("_", 1)[1]
    m = _JAR.match(base)
    return (m["artifact"], m["version"]) if m else None


def bundled_jars() -> dict[str, str]:
    """artifact → version for the jars shipped inside the pyspark package."""
    import pyspark
    jars = Path(pyspark.__file__).parent / "jars"
    return dict(c for c in (jar_coordinates(p.name, ivy=False) for p in jars.glob("*.jar")) if c)


def duplicates(package_jars: list[str], bundled: dict[str, str]) -> list[tuple[str, str, str]]:
    """(artifact, package version, bundled version) for every package jar whose artifact pyspark also ships."""
    out = []
    for j in package_jars:
        c = jar_coordinates(j, ivy=True)
        if c and c[0] in bundled:
            out.append((c[0], c[1], bundled[c[0]]))
    return sorted(out)


def jar_of(java_object) -> str:
    """The jar a live JVM object's class was loaded from."""
    return str(java_object.getClass().getProtectionDomain().getCodeSource().getLocation().toString())


def classpath_report(spark: "SparkSession") -> dict:
    """Package jars, their duplicates in pyspark, and where Hadoop and s3a actually load from."""
    jars = [j for j in spark.sparkContext.getConf().get("spark.jars", "").split(",") if j]
    hconf = spark.sparkContext._jsc.hadoopConfiguration()
    jvm = spark.sparkContext._jvm
    return {
        "package_jars": len(jars),
        "duplicates": duplicates(jars, bundled_jars()),
        "hadoop_version": str(jvm.org.apache.hadoop.util.VersionInfo.getVersion()),
        "hadoop_jar": jar_of(hconf),
    }
