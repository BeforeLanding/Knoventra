from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, ValidationInfo, field_validator
from typing import Literal


class ModelConfigurationError(RuntimeError):
    """Invalid model selection or unsupported changes to a loaded encoder."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KNV_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://knoventra:knoventra@localhost:55432/knoventra"
    jwt_secret: str = "dev-only-change-me-dev-only-change-me"
    jwt_ttl_minutes: int = 480
    env: str = "dev"
    upload_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    chat_max_chars: int = Field(default=4000, gt=0)
    # "bge-m3" loads the real model (needs the `ml` extra); "hash" is a deterministic
    # offline encoder used by tests and CI.
    encoder: Literal["hash", "bge-m3"] = "hash"
    dense_dim: int = Field(default=1024, gt=0)
    sparse_dim: int = Field(default=250002, gt=0)  # BGE-M3 XLM-R vocabulary size
    recall_top_k: int = 20
    final_top_k: int = 5
    rrf_k: int = 60
    rerank: Literal["off", "bge"] = "off"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_candidates: int = 10
    # A separate sigmoid-score scale; 0.60 preserves the previous rerank default,
    # not a calibrated probability of answerability. Rerank remains opt-in.
    rerank_threshold: float = 0.60
    # Evidence gate on dense cosine (or rerank score). 0.60 is tuned on the BGE-M3 dev split only (see eval report).
    gate_threshold: float = 0.60
    gate_sparse_threshold: float = 0.03  # sparse inner-product arm; calibrated on dev + val2 (in-sample)
    max_tool_calls_per_turn: int = 4
    confirm_ttl_minutes: int = 30
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 60.0

    @field_validator("rerank_model")
    @classmethod
    def active_reranker_requires_name(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("rerank") == "bge" and not value.strip():
            raise ValueError("rerank_model must be nonblank when rerank=bge")
        return value


settings = Settings()
