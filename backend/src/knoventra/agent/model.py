"""Shared explicit provider options for the web and evaluation chat models."""
from knoventra.config import ModelConfigurationError, settings


def chat_model_kwargs() -> dict:
    options = {"base_url": settings.llm_base_url or None, "api_key": settings.llm_api_key or "none",
               "model": settings.llm_model, "timeout": settings.llm_timeout_s}
    if settings.llm_thinking == "disabled":
        options["extra_body"] = {"thinking": {"type": "disabled"}}
    elif settings.llm_thinking != "provider-default":
        raise ModelConfigurationError("unsupported llm_thinking mode")
    return options
