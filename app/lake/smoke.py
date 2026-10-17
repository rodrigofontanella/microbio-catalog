"""Prove the lake works end to end from Leeloo (Infrastructure acceptance test 4). Prints no secrets.

    python -m app.lake.smoke

Checks the classpath first: exactly one Hadoop (the one bundled in pyspark, no hadoop-* among the package jars),
and every package jar that pyspark also bundles is listed. Then writes a 10-row Delta table to
<LAKE_URI>/_smoke/delta_roundtrip, reads it back, shows its history, reads
<COLD_URI>/raw/pgmd/v1/pgmd-v1.SHA256SUMS, checks that s3a loads from hadoop-aws of the same Hadoop version,
then deletes _smoke/. Exit 0 only if every check passes.
The first run downloads the Delta and hadoop-aws jars (and the AWS SDK bundle) into the Ivy cache.
"""
from __future__ import annotations

import sys

from pyspark.sql.types import LongType, StringType, StructField, StructType

from app.config import settings
from app.lake.spark import build_spark, classpath_report, jar_of, redacted, spark_conf

SCHEMA = StructType([StructField("id", LongType(), False), StructField("label", StringType(), False)])


def main() -> int:
    conf = spark_conf(settings)
    print("config:", {k: v for k, v in redacted(conf).items() if "s3a" in k or k in (
        "spark.master", "spark.local.dir", "spark.jars.packages", "spark.jars.excludes")}, flush=True)
    spark = build_spark("lake-smoke")
    jvm = spark.sparkContext._jvm
    print(f"spark {spark.version} · hadoop {jvm.org.apache.hadoop.util.VersionInfo.getVersion()} · "
          f"java {jvm.java.lang.System.getProperty('java.version')}", flush=True)

    ok = True
    cp = classpath_report(spark)
    print(f"package jars: {cp['package_jars']}; also bundled in pyspark: {len(cp['duplicates'])}")
    for artifact, pkg, bundled in cp["duplicates"]:
        print(f"  {artifact} {pkg} (pyspark {bundled})")
    hadoop_dupes = [d for d in cp["duplicates"] if d[0].startswith("hadoop-")]
    print(f"hadoop {cp['hadoop_version']} from {cp['hadoop_jar'].rsplit('/', 1)[-1]}; "
          f"hadoop jars among the packages: {len(hadoop_dupes)}")
    ok &= cp["package_jars"] > 0 and not hadoop_dupes
    ok &= cp["hadoop_jar"].endswith(f"/hadoop-client-api-{cp['hadoop_version']}.jar")

    path = f"{settings.lake_uri}/_smoke/delta_roundtrip"
    spark.createDataFrame([(i, f"row-{i}") for i in range(10)], SCHEMA) \
        .write.format("delta").mode("overwrite").save(path)
    n = spark.read.format("delta").load(path).count()
    print(f"rows read back: {n}")
    ok &= n == 10
    history = spark.sql(f"DESCRIBE HISTORY delta.`{path}`").select("version", "operation").collect()
    print("history:", [(r.version, r.operation) for r in history])
    ok &= [(r.version, r.operation) for r in history] == [(0, "WRITE")]

    sums = spark.read.text(f"{settings.cold_uri}/raw/pgmd/v1/pgmd-v1.SHA256SUMS").collect()
    print(f"cold readable: {len(sums)} lines")
    ok &= len(sums) == 5

    fs_path = jvm.org.apache.hadoop.fs.Path(f"{settings.lake_uri}/_smoke")
    fs = fs_path.getFileSystem(spark.sparkContext._jsc.hadoopConfiguration())
    s3a_jar = jar_of(fs).rsplit("/", 1)[-1]
    print(f"s3a from {s3a_jar}")
    ok &= s3a_jar.endswith(f"hadoop-aws-{cp['hadoop_version']}.jar")
    deleted = fs.delete(fs_path, True)
    print(f"_smoke deleted: {deleted}")
    ok &= bool(deleted) and not fs.exists(fs_path)
    spark.stop()
    print("SMOKE OK" if ok else "SMOKE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
