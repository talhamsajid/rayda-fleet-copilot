from __future__ import annotations

import hashlib
import json
import os
import time
from functools import lru_cache
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage, messages_from_dict, messages_to_dict

from .data import ROOT

DEFAULT_MODEL = "anthropic:claude-opus-5-5"

PROVIDERS = {
    "anthropic": {"env": "ANTHROPIC_API_KEY"},
    "openai": {"env": "OPENAI_API_KEY", "base_url": None},
    "gemini": {"env": "GEMINI_API_KEY"},
    "deepseek": {"env": "DEEPSEEK_API_KEY", "base_url": "https://api.deepseek.com"},
    "kimi": {"env": "MOONSHOT_API_KEY", "base_url": "https://api.moonshot.ai/v1"},
    "qwen": {"env": "DASHSCOPE_API_KEY", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
    "openai_compat": {"env": "FLEET_API_KEY", "base_url_env": "FLEET_BASE_URL"},
}

ANTHROPIC_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1"}
NO_TEMPERATURE_PREFIXES = ("gpt-5", "o1", "o3", "o4")

PRICES = {"anthropic:claude-opus-5-5": (4.0, 20.0), "anthropic:claude-sonnet-5-5": (2.0, 10.0),
          "anthropic:claude-haiku-5-5": (0.10, 0.50), "gemini:gemini-3.6-flash": (1.50, 7.50)}

USAGE: list[dict] = []


def cost(spec: str, input_tokens: int, output_tokens: int) -> float | None:
    if spec not in PRICES:
        return None
    pin, pout = PRICES[spec]
    return round((input_tokens * pin + output_tokens * pout) / 1e6, 4)


def usage_of(msg: AIMessage) -> dict:
    u = msg.usage_metadata or {}
    return {"model": msg.response_metadata.get("fleet_model", current_model()),
            "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
            "latency_s": msg.response_metadata.get("fleet_latency_s"),
            "replayed": msg.response_metadata.get("fleet_replayed", False)}


class CassetteMiss(RuntimeError):
    pass


def current_model() -> str:
    return os.environ.get("FLEET_MODEL", DEFAULT_MODEL)


def parse_model(spec: str) -> tuple[str, str]:
    provider, _, model = spec.partition(":")
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Use one of: {', '.join(PROVIDERS)}")
    if not model:
        raise ValueError(f"Give a model id: FLEET_MODEL={provider}:<model> (see the provider's /v1/models list)")
    return provider, model


def _rate_limiter():
    rpm = os.environ.get("FLEET_RPM")
    if not rpm:
        return None
    from langchain_core.rate_limiters import InMemoryRateLimiter

    return InMemoryRateLimiter(requests_per_second=float(rpm) / 60, check_every_n_seconds=0.2)


@lru_cache(maxsize=8)
def chat_model(spec: str):
    provider, model = parse_model(spec)
    cfg = PROVIDERS[provider]
    limiter = _rate_limiter()
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not os.environ.get(cfg["env"]):
            raise RuntimeError(f"Set {cfg['env']} to use provider 'gemini'.")
        kwargs = {"temperature": float(t)} if (t := os.environ.get("FLEET_TEMPERATURE")) else {}
        return ChatGoogleGenerativeAI(model=model, google_api_key=os.environ[cfg["env"]], max_retries=6, timeout=300,
                                      rate_limiter=limiter, **kwargs)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs = {}
        if effort := os.environ.get("FLEET_EFFORT", "medium"):
            kwargs["effort"] = effort
        if model in ANTHROPIC_FALLBACK_MODELS:
            kwargs |= {"betas": ["server-side-fallback-2026-07-01"], "model_kwargs": {"fallbacks": "default"}}
        return ChatAnthropic(model=model, max_tokens=16000, max_retries=3, timeout=300, rate_limiter=limiter, **kwargs)

    from langchain_openai import ChatOpenAI

    base_url = os.environ.get(cfg["base_url_env"]) if "base_url_env" in cfg else cfg["base_url"]
    api_key = os.environ.get(cfg["env"]) or ("not-needed" if provider == "openai_compat" else None)
    if not api_key:
        raise RuntimeError(f"Set {cfg['env']} to use provider '{provider}'.")
    temperature = os.environ.get("FLEET_TEMPERATURE", "0")
    kwargs = {}
    if temperature != "none" and not (provider == "openai" and model.startswith(NO_TEMPERATURE_PREFIXES)):
        kwargs["temperature"] = float(temperature)
    # DeepSeek/Kimi thinking variants need reasoning_content echoed back, which this client drops; use their
    # native LangChain packages for those. This path targets their non-thinking chat models.
    return ChatOpenAI(model=model, base_url=base_url, api_key=api_key, max_retries=3, timeout=300,
                      use_responses_api=False if provider != "openai" else None, rate_limiter=limiter, **kwargs)


def _normalize(m: BaseMessage) -> dict:
    d = {"type": m.type, "content": m.content}
    if isinstance(m, AIMessage):
        d["tool_calls"] = [{"name": c["name"], "args": c["args"], "id": c["id"]} for c in m.tool_calls]
    if isinstance(m, ToolMessage):
        d["tool_call_id"] = m.tool_call_id
    return d


def request_key(spec: str, messages: list[BaseMessage], tools: list[dict]) -> str:
    payload = {"model": spec, "tools": tools, "messages": [_normalize(m) for m in messages]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _without(schema, key: str):
    if isinstance(schema, dict):
        return {k: _without(v, key) for k, v in schema.items() if k != key}
    if isinstance(schema, list):
        return [_without(v, key) for v in schema]
    return schema


def cassette_dir() -> Path:
    return Path(os.environ.get("FLEET_CASSETTES", ROOT / "evals" / "cassettes"))


def invoke(messages: list[BaseMessage], tools: list[dict]) -> AIMessage:
    spec = current_model()
    mode = os.environ.get("FLEET_LLM_MODE", "live")
    key = request_key(spec, messages, tools)
    path = cassette_dir() / spec.replace(":", "__").replace("/", "_") / f"{key}.json"
    if mode in ("replay", "record") and path.exists():
        rec = json.loads(path.read_text())
        msg = messages_from_dict([rec["message"]])[0]
        msg.response_metadata |= {"fleet_model": spec, "fleet_latency_s": rec["latency_s"], "fleet_replayed": True}
        USAGE.append({"model": spec, "replayed": True, "latency_s": rec["latency_s"], **(msg.usage_metadata or {})})
        return msg
    if mode == "replay":
        raise CassetteMiss(f"No recorded response for request {key} on {spec}. Run `make eval-record` with an API key.")
    start = time.monotonic()
    bound = _without(tools, "additionalProperties") if spec.startswith("gemini:") else tools
    msg = chat_model(spec).bind_tools(bound, tool_choice="auto").invoke(messages)
    latency = round(time.monotonic() - start, 2)
    stored = messages_to_dict([msg])[0]
    if mode == "record":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, "model": spec, "latency_s": latency, "message": stored},
                                   indent=1, sort_keys=True, default=str))
    msg = messages_from_dict([json.loads(json.dumps(stored, default=str))])[0]
    msg.response_metadata |= {"fleet_model": spec, "fleet_latency_s": latency, "fleet_replayed": False}
    USAGE.append({"model": spec, "replayed": False, "latency_s": latency, **(msg.usage_metadata or {})})
    return msg
