# app/config.py
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    ncbi_api_key: str = ""
    ncbi_base_url: str = "https://api.ncbi.nlm.nih.gov/datasets/v2"
    database_url: str = "postgresql+asyncpg://ncbi_user:ncbi_pass@localhost:5432/ncbi_db"
    test_database_url: str = "postgresql+asyncpg://ncbi_user:ncbi_pass@localhost:5432/ncbi_test_db"

    # E-utilities (catalog ingest). The same NCBI API key serves Datasets and E-utilities.
    eutils_base_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    ncbi_email: str = ""            # NCBI asks for a contact address with E-utilities calls
    ncbi_tool: str = "ncbi-api"
    # Where raw NCBI responses are written (one timestamped folder per fetch). Required by ingests.
    raw_dir: str = ""

    # Object store (Silo on Skipper, S3 API) and Spark (local mode on Leeloo). Secrets come from .env only.
    s3_endpoint: str = ""                  # e.g. http://192.168.2.10:9000
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_region: str = "us-east-1"
    lake_uri: str = "s3a://lake"           # Delta tables
    cold_uri: str = "s3a://cold"           # raw downloads
    spark_master: str = "local[2]"
    spark_driver_memory: str = "2g"
    spark_shuffle_partitions: int = 4
    spark_local_dir: str = ""              # shuffle scratch on NVMe, e.g. /data/spark/local
    # must match: delta-spark (pip) ↔ delta-spark_2.13 jar; hadoop-aws ↔ the Hadoop bundled in pyspark
    spark_packages: str = "io.delta:delta-spark_2.13:4.4.0,org.apache.hadoop:hadoop-aws:3.5.0"
    # delta-kernel-defaults pulls hadoop-client-* 3.4.2; pyspark bundles 3.5.0, which hadoop-aws 3.5.0 needs.
    # Excluded so exactly one Hadoop is on the classpath (smoke test enforces it). groupId:artifactId, comma-separated.
    spark_jars_excludes: str = "org.apache.hadoop:hadoop-client-api,org.apache.hadoop:hadoop-client-runtime"
    # PGMD v1 objects under COLD_URI (Microbial.csv + pgmd-v1.SHA256SUMS), uploaded by Infrastructure
    pgmd_raw_prefix: str = "raw/pgmd/v1"
    # depth flag on core.profile_run (design doc §2.1.1): fail < DEPTH_FAIL_BELOW <= low < DEPTH_LOW_BELOW <= ok.
    # Recorded on every row; changing them re-flags only with pgmd_profile --reflag-depth.
    depth_fail_below: int = 1000
    depth_low_below: int = 5000

    model_config = SettingsConfigDict(env_file=".env")

settings = Settings()
