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
    rerank: str = "off"  # off | bge
    rerank_candidates: int = 10
    # Evidence gate on dense cosine (or rerank score). 0.60 is tuned on the BGE-M3 dev split only (see eval report).
    gate_threshold: float = 0.60
    max_tool_calls_per_turn: int = 4
    confirm_ttl_minutes: int = 30
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 60.0


settings = Settings()
