"""Vendor adapters for the Gateway plane. connexa-echo always exists (deterministic,
zero-credential fallback model); OpenAI/Anthropic adapters activate when keys exist.
"""
from __future__ import annotations

import json
import time
import httpx

TOKENS_PER_CHAR = 4.0   # ~4 chars per token estimate


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text or "") / TOKENS_PER_CHAR))


def echo_generate(prompt_text: str, max_tokens: int) -> str:
    """Deterministic local model: reflects the prompt so agents can test end-to-end."""
    snippet = " ".join(prompt_text.split())[:280]
    return (f"[connexa-echo] You said: \u201c{snippet}\u201d "
            f"(echo model; configure OPENAI_API_KEY / ANTHROPIC_API_KEY for live vendors).")


def openai_generate(model: str, messages: list[dict], max_tokens: int, api_key: str) -> dict:
    """Calls OpenAI chat completions. Returns {"text", "input_tokens", "output_tokens", "model"}.
    Raises on failure so the router can fail over to the next model in policy."""
    with httpx.Client(timeout=45.0) as client:
        response = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages,
                  "max_tokens": max_tokens if max_tokens > 0 else 256},
        )
        response.raise_for_status()
        data = response.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message", {})
    usage_row = data.get("usage", {})
    return {"text": message.get("content", ""),
            "input_tokens": usage_row.get("prompt_tokens") or 0,
            "output_tokens": usage_row.get("completion_tokens") or 0,
            "model": data.get("model", model)}


def anthropic_generate(model: str, messages: list[dict], max_tokens: int, api_key: str) -> dict:
    """Calls Anthropic /v1/messages with the same messages shape, returns normalized result."""
    system_bits = []
    convo = []
    for message in messages:
        if message.get("role") == "system":
            system_bits.append(str(message.get("content", "")))
        else:
            convo.append({"role": message.get("role"), "content": str(message.get("content", ""))})
    payload: dict = {"model": model, "messages": convo,
                     "max_tokens": max_tokens if max_tokens > 0 else 256}
    if system_bits:
        payload["system"] = "\n".join(system_bits)
    with httpx.Client(timeout=45.0) as client:
        response = client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json=payload)
        response.raise_for_status()
        data = response.json()
    text = "".join(block.get("text", "") for block in data.get("content", []))
    usage_row = data.get("usage", {})
    return {"text": text, "input_tokens": usage_row.get("input_tokens") or 0,
            "output_tokens": usage_row.get("output_tokens") or 0, "model": data.get("model", model)}
