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
    # Elasticsearch keyword channel; empty URL disables it (pgvector channels only).
    es_url: str = ""
    es_index: str = "knv_chunks"
    es_timeout_s: float = Field(default=2.0, gt=0)
    recall_top_k: int = 20
    final_top_k: int = 5
    rrf_k: int = 60
    # Weighted RRF: score = sum_c w_c / (rrf_k + rank_c). Two weight sets by query type;
    # "equal" keeps the v3 behaviour as a baseline. Tuned on the dev split only (D5).
    rrf_weighting: Literal["weighted", "equal"] = "weighted"
    rrf_weights_identifier: dict[str, float] = {"ident": 2.0, "es": 1.5, "sparse": 1.0, "dense": 0.5}
    # Chosen on dev (D5): ties every grid point on MRR, +1 of 22 on source recall. Weak evidence.
    rrf_weights_natural: dict[str, float] = {"dense": 1.0, "sparse": 0.5, "es": 1.5, "ident": 1.0}
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
    # Query planning before the agent ("off" keeps v3 behaviour). Routing narrows scope only.
    query_plan: Literal["off", "llm"] = "llm"
    query_plan_timeout_s: float = Field(default=8.0, gt=0)
    query_plan_min_confidence: float = Field(default=0.6, ge=0, le=1)
    intent_kbs: dict[str, list[str]] = {
        "case_sop": ["ops-global", "ops-cn", "field-sop", "field-cases"],
        "policy": ["hr", "fin"],
        "it_guide": ["it-public"],
    }
    # Short-term working memory (Redis); empty URL disables it and every read rebuilds from
    # the checkpoint. Redis is a cache, never the source of truth.
    redis_url: str = ""
    redis_timeout_s: float = Field(default=0.5, gt=0)
    working_memory_ttl_s: int = Field(default=24 * 3600, gt=0)  # renewed on every access
    # History sent to the answer model: over this approximate token budget, older turns are
    # summarized and the last `context_keep_turns` turns are kept verbatim (>= 2 so an open
    # slot draft and its follow-up question are never summarized away).
    context_budget_tokens: int = Field(default=6000, gt=0)
    context_keep_turns: int = Field(default=3, ge=2)
    summary_timeout_s: float = Field(default=20.0, gt=0)
    summary_max_chars: int = Field(default=1500, gt=0)
    # Long-term episodic memory (D12): per-user recall of earlier answers and resolved help
    # requests; every recalled episode is re-checked against current permissions before use.
    episodic_memory: bool = True
    episode_recall_k: int = Field(default=3, ge=1, le=10)
    # Dense cosine floor. Not calibrated: no paid memory evaluation has been run yet.
    episode_min_score: float = Field(default=0.5, ge=0, le=1)
    episode_chars: int = Field(default=600, gt=0)
    # Tool execution over MCP (D13). Empty URL runs tools in-process (tests, eval, single box).
    mcp_url: str = ""
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 58765
    mcp_timeout_s: float = Field(default=30.0, gt=0)
    # Per-run identity tokens for the MCP server: own secret, short life (one HTTP request).
    tool_token_secret: str = "dev-only-tool-token-secret-change-me"
    tool_token_ttl_s: int = Field(default=300, gt=0)
    confirm_ttl_minutes: int = 30
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 60.0
    # Opt in only for providers accepting DeepSeek's thinking switch. The generic
    # OpenAI adapter does not preserve provider-specific reasoning tool history.
    llm_thinking: Literal["provider-default", "disabled"] = "provider-default"

    @field_validator("rerank_model")
    @classmethod
    def active_reranker_requires_name(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("rerank") == "bge" and not value.strip():
            raise ValueError("rerank_model must be nonblank when rerank=bge")
        return value


settings = Settings()
