"""Spark session config: s3a + Delta wiring from settings, secrets redacted. No JVM, no network."""
from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.lake.spark import redacted, spark_conf


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
