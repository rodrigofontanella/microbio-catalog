"""One way to build the Spark session: local mode on Leeloo, Delta on the S3-compatible store on Skipper.

Everything comes from settings (.env), nothing is hardcoded:
    S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION, LAKE_URI, COLD_URI,
    SPARK_MASTER, SPARK_DRIVER_MEMORY, SPARK_SHUFFLE_PARTITIONS, SPARK_LOCAL_DIR, SPARK_PACKAGES

Sizing: local[2], 2 GB driver, 4 shuffle partitions. The data is tens of thousands of rows per study and ~2M
for all of PGMD, so the defaults (200 shuffle partitions, all cores) would only add overhead.
Delta on S3: ONE writer per table at a time. With one driver the default S3 log store is safe; two drivers
writing the same table concurrently can lose a commit.
"""
from __future__ import annotations

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
