"""Capa de IA local OPCIONAL via Ollama: shim del nucleo compartido
(octonove_core.llm). Esta app gana respecto a su copia antigua: 127.0.0.1 +
OLLAMA_HOST (fix del retraso IPv6), reintento 2x en generate y filtro de
modelos 'embed' en la autodeteccion. Defaults de generate: 120 s / temp 0.3
(los del core)."""

from octonove_core.llm import (  # noqa: F401
    OLLAMA_URL,
    _cache,
    _get,
    _resolve_ollama_url,
    available,
    default_model,
    generate,
    has_gpu,
    list_models,
    recommend_model,
    reset_cache,
    set_model,
    system_ram_gb,
)
