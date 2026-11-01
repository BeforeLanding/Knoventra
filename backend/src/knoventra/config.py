from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KNV_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://knoventra:knoventra@localhost:55432/knoventra"
    jwt_secret: str = "dev-only-change-me-dev-only-change-me"
    jwt_ttl_minutes: int = 480
    env: str = "dev"
    # "bge-m3" loads the real model (needs the `ml` extra); "hash" is a deterministic
    # offline encoder used by tests and CI.
    encoder: str = "hash"
    dense_dim: int = 1024
    sparse_dim: int = 250002  # BGE-M3 XLM-R vocabulary size
    recall_top_k: int = 20
    final_top_k: int = 5
    rrf_k: int = 60


settings = Settings()
