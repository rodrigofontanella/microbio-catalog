"""Spark session config: s3a + Delta wiring from settings, secrets redacted. No JVM, no network."""
from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.lake.spark import duplicates, jar_coordinates, redacted, spark_conf


def _settings(**over) -> Settings:
    base = dict(s3_endpoint="http://192.168.2.10:9000", s3_access_key=SecretStr("AKIA-TEST"),
                s3_secret_key=SecretStr("s3cr3t-TEST"), spark_local_dir="/data/spark/local")
    return Settings(_env_file=None, **{**base, **over})


def test_s3a_and_delta_wiring():
    c = spark_conf(_settings())
    assert c["spark.hadoop.fs.s3a.path.style.access"] == "true"
    assert c["spark.hadoop.fs.s3a.connection.ssl.enabled"] == "false"          # http:// endpoint
    assert c["spark.hadoop.fs.s3a.endpoint.region"] == "us-east-1"
    assert c["spark.sql.extensions"] == "io.delta.sql.DeltaSparkSessionExtension"
    assert c["spark.local.dir"] == "/data/spark/local"
    assert (c["spark.master"], c["spark.driver.memory"], c["spark.sql.shuffle.partitions"]) == ("local[2]", "2g", "4")


def test_https_endpoint_turns_ssl_on():
    assert spark_conf(_settings(s3_endpoint="https://s3.example"))["spark.hadoop.fs.s3a.connection.ssl.enabled"] == "true"


def test_jar_pins_match_the_pip_pins():
    import importlib.metadata as md
    packages = spark_conf(_settings())["spark.jars.packages"]
    assert f"io.delta:delta-spark_2.13:{md.version('delta-spark')}" in packages
    assert md.version("pyspark") == "4.2.0" and "hadoop-aws:3.5.0" in packages    # pyspark 4.2.0 bundles Hadoop 3.5.0


def test_secrets_are_redacted_and_never_in_settings_repr():
    s = _settings()
    assert "s3cr3t-TEST" not in repr(s) and "AKIA-TEST" not in repr(s)
    r = redacted(spark_conf(s))
    assert r["spark.hadoop.fs.s3a.secret.key"] == "***" and "s3cr3t-TEST" not in str(r)


def test_missing_credentials_are_named():
    with pytest.raises(ValueError, match="S3_SECRET_KEY"):
        spark_conf(_settings(s3_secret_key=SecretStr("")))


def test_delta_s_hadoop_client_is_excluded():
    c = spark_conf(_settings())
    assert c["spark.jars.excludes"].split(",") == ["org.apache.hadoop:hadoop-client-api",
                                                    "org.apache.hadoop:hadoop-client-runtime"]
    assert "spark.jars.excludes" not in spark_conf(_settings(spark_jars_excludes=""))


def test_jar_names_parse_for_ivy_and_pyspark():
    assert jar_coordinates("file:///h/.ivy2.5.2/jars/org.apache.hadoop_hadoop-client-api-3.4.2.jar", ivy=True) == \
        ("hadoop-client-api", "3.4.2")
    assert jar_coordinates("commons-pool_commons-pool-1.6.jar", ivy=True) == ("commons-pool", "1.6")
    assert jar_coordinates("io.delta_delta-spark_2.13-4.4.0.jar", ivy=True) == ("delta-spark_2.13", "4.4.0")
    assert jar_coordinates("com.github.luben_zstd-jni-1.5.7-3.jar", ivy=True) == ("zstd-jni", "1.5.7-3")
    assert jar_coordinates("jackson-module-scala_2.13-2.21.2.jar", ivy=False) == ("jackson-module-scala_2.13", "2.21.2")
    assert jar_coordinates("no-version.jar", ivy=False) is None


def test_duplicates_name_both_versions():
    bundled = {"hadoop-client-api": "3.5.0", "parquet-hadoop": "1.17.0"}
    jars = ["file:/j/org.apache.hadoop_hadoop-client-api-3.4.2.jar", "file:/j/org.apache.parquet_parquet-hadoop-1.16.0.jar",
            "file:/j/org.apache.hadoop_hadoop-aws-3.5.0.jar"]
    assert duplicates(jars, bundled) == [("hadoop-client-api", "3.4.2", "3.5.0"), ("parquet-hadoop", "1.16.0", "1.17.0")]
