"""Nebius Token Factory client with Nemotron model routing.

Nano handles fast, cheap calls (classification, extraction), Super handles
everyday reasoning, Ultra is reserved for the hard decisions. Model IDs are
discovered from the /models endpoint so the app keeps working if the catalogue
changes.
"""
import json
import os
import re
import time

from dotenv import load_dotenv
from json_repair import repair_json
from openai import OpenAI

load_dotenv()

BASE_URL = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")

client = OpenAI(base_url=BASE_URL, api_key=os.getenv("NEBIUS_API_KEY", "missing"))

# Fallback IDs; overridden by env vars or by discovery.
MODELS = {
    "nano": os.getenv("MODEL_NANO", ""),
    "super": os.getenv("MODEL_SUPER", ""),
    "ultra": os.getenv("MODEL_ULTRA", ""),
}

# Running usage log so the UI can show which model did what and what it cost.
USAGE: list[dict] = []


def discover_models() -> dict:
    """Fill MODELS with the Nemotron IDs available on this account."""
    try:
        ids = [m.id for m in client.models.list().data]
    except Exception as e:  # key missing or network down
        print(f"[llm] model discovery failed: {e}")
        return MODELS
    nemotron = [i for i in ids if "nemotron" in i.lower()]
    for tier in ("nano", "super", "ultra"):
        if MODELS[tier]:
            continue
        hits = [i for i in nemotron if tier in i.lower() and "embed" not in i.lower()]
        if hits:
            MODELS[tier] = sorted(hits)[-1]
    # Any tier still empty falls back to the nearest available one.
    for tier, fallbacks in (("super", ("ultra", "nano")), ("ultra", ("super", "nano")), ("nano", ("super", "ultra"))):
        if not MODELS[tier]:
            MODELS[tier] = next((MODELS[f] for f in fallbacks if MODELS[f]), "")
    print(f"[llm] routing: {MODELS}")
    return MODELS


def chat(tier: str, messages: list[dict], *, json_mode: bool = False, max_tokens: int = 2048,
         temperature: float = 0.2, step: str = "", think: bool = False) -> str:
    """Call the model for `tier` and log latency/tokens for the routing panel.

    Nemotron 3 reasons by default; we only pay for that where the step needs it.
    """
    model = MODELS.get(tier) or MODELS["super"]
    kwargs = {"extra_body": {"chat_template_kwargs": {"enable_thinking": think}}}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    t0 = time.time()
    resp = client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, temperature=temperature, **kwargs
    )
    text = resp.choices[0].message.content or ""
    u = resp.usage
    details = getattr(u, "completion_tokens_details", None)
    USAGE.append({
        "step": step, "tier": tier, "model": model, "thinking": think,
        "latency_s": round(time.time() - t0, 2),
        "prompt_tokens": getattr(u, "prompt_tokens", 0),
        "completion_tokens": getattr(u, "completion_tokens", 0),
        "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0,
    })
    return strip_reasoning(text)


VISION_MODEL = os.getenv("MODEL_VISION", "nvidia/Nemotron-3.5-Super-VL")


def read_image(image_b64: str, mime: str) -> tuple[str, dict]:
    """Transcribe a photographed letter with Nemotron VL, redacting identifiers."""
    t0 = time.time()
    resp = client.chat.completions.create(
        model=VISION_MODEL, max_tokens=1500, temperature=0,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "Transcribe this medical letter as plain text. Replace the patient's name, "
                                     "date of birth, NHS number, address and phone number with [redacted]. "
                                     "Output only the transcription."},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ]}],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    u = resp.usage
    usage = {"step": "Read letter photo", "tier": "vision", "model": VISION_MODEL, "thinking": False,
             "latency_s": round(time.time() - t0, 2), "prompt_tokens": u.prompt_tokens,
             "completion_tokens": u.completion_tokens, "reasoning_tokens": 0}
    return strip_reasoning(resp.choices[0].message.content or ""), usage


def chat_json(tier: str, messages: list[dict], **kw) -> dict:
    """Like chat() but returns parsed JSON, tolerating code fences around it."""
    text = chat(tier, messages, json_mode=True, **kw)
    return parse_json(text)


def strip_reasoning(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def parse_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        # Models occasionally leave an unescaped quote in a long string.
        return repair_json(text[start:end + 1], return_objects=True)
