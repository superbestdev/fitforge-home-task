"""Central configuration.

Everything the system needs to talk to is expressed as a URL plus a model name,
so moving from Ollama to vLLM (or to a hosted endpoint, if the no-paid-services
constraint is ever lifted) is an .env change rather than a code change.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- infrastructure ---------------------------------------------------
    database_url: str = "postgresql://fitforge:fitforge@localhost:5432/fitforge"
    redis_url: str = "redis://localhost:6379/0"

    # --- llm --------------------------------------------------------------
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5:3b-instruct"
    # Intent classification runs many times per session and needs no depth, so
    # it gets a smaller model. This is the single biggest latency win on CPU.
    llm_router_model: str = "qwen2.5:1.5b-instruct"
    llm_temperature: float = 0.2
    llm_timeout_s: int = 180
    llm_max_retries: int = 2

    embed_base_url: str = "http://localhost:11434"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768

    # --- agent behaviour --------------------------------------------------
    # How many diagnostic steps one issue thread gets before we admit defeat.
    # Bounded loops are the difference between "iterative troubleshooting" and
    # "an agent that asks about the power cable for twenty turns".
    diagnostic_step_budget: int = 8
    # Troubleshooting is gated on identifying the model. Below this confidence
    # we keep asking rather than guess.
    model_id_confidence_threshold: float = 0.85
    # If the best retrieved chunk scores below this, we do not actually know the
    # answer for this model — escalate instead of improvising.
    retrieval_min_score: float = 0.28
    payment_human_approval_cents: int = 50_000
    max_tool_failures_per_issue: int = 3

    # --- retrieval --------------------------------------------------------
    retrieval_top_k: int = 6
    retrieval_candidates: int = 40
    rrf_k: int = 60

    # --- services ---------------------------------------------------------
    mock_psp_url: str = "http://localhost:8090"

    # --- observability ----------------------------------------------------
    langfuse_enabled: bool = False
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # --- seed -------------------------------------------------------------
    seed_random_seed: int = 20260827
    seed_num_models: int = 312
    seed_scanned_ratio: float = 0.15
    seed_missing_ratio: float = 0.05

    # --- app --------------------------------------------------------------
    log_level: str = "INFO"
    environment: str = "local"
    manuals_dir: str = "/app/data/manuals"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
