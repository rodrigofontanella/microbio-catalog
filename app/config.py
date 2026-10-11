# app/config.py
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

    model_config = SettingsConfigDict(env_file=".env")

settings = Settings()
