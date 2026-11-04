#!/usr/bin/env python3
"""Which Meta models can this project use? Lists them and tests tool calling.

    python agents/check_models.py                 # list models on both providers, test the configured open model
    python agents/check_models.py --model <id>    # test a specific Baseten model id

Reads MODEL_API_KEY and BASETEN_API_KEY (plus optional MODEL_API_BASE, BASETEN_API_BASE,
MUSE_SPARK_OPEN_MODEL) from .env. Keys are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

INTERESTING = ("muse", "glimmer", "llama", "meta")


def list_models(name: str, client: OpenAI) -> list[str]:
    try:
        ids = sorted(m.id for m in client.models.list())
    except Exception as e:                      # some endpoints don't offer a model list
        print(f"[{name}] could not list models: {type(e).__name__}: {str(e)[:200]}")
        return []
    meta = [i for i in ids if any(k in i.lower() for k in INTERESTING)]
    print(f"[{name}] {len(ids)} models, Meta ones:")
    for i in meta or ["(none matched muse/glimmer/llama/meta)"]:
        print(f"    {i}")
    return meta


def test_chat_tools(client: OpenAI, model: str) -> bool:
    """Same three steps as check_api.py, but over Chat Completions (what Baseten serves)."""
    print(f"\n[baseten] testing {model}")
    try:
        r = client.chat.completions.create(model=model, max_tokens=50,
                                           messages=[{"role": "user", "content": "Reply with exactly: GridTwin online"}])
    except Exception as e:
        print(f"[1] plain call    -> FAILED: {type(e).__name__}: {str(e)[:300]}")
        return False
    print(f"[1] plain call    -> {(r.choices[0].message.content or '').strip()!r}  ({r.usage.total_tokens} tokens)")

    tools = [{"type": "function", "function": {
        "name": "read_battery", "description": "Read the emulated battery's cell voltages in millivolts.",
        "parameters": {"type": "object", "properties": {}}}}]
    messages = [{"role": "user", "content": "Is any cell above 3650 mV? Use the tool to check."}]
    r = client.chat.completions.create(model=model, messages=messages, tools=tools)
    msg = r.choices[0].message
    if not msg.tool_calls:
        print(f"[2] tool call     -> FAILED, answered without the tool: {(msg.content or '')[:200]!r}")
        return False
    c = msg.tool_calls[0]
    print(f"[2] tool call     -> model called {c.function.name}({c.function.arguments})")

    messages += [{"role": "assistant", "content": msg.content or None,
                  "tool_calls": [{"id": c.id, "type": "function",
                                  "function": {"name": c.function.name, "arguments": c.function.arguments}}]},
                 {"role": "tool", "tool_call_id": c.id, "content": json.dumps({"cells_mv": [3301, 3304, 3307, 3708]})}]
    r = client.chat.completions.create(model=model, messages=messages, tools=tools)
    print(f"[3] tool result   -> {(r.choices[0].message.content or '').strip()[:300]}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="Baseten model id to test")
    args = ap.parse_args()

    if os.environ.get("MODEL_API_KEY", "").strip():
        list_models("meta", OpenAI(base_url=os.environ.get("MODEL_API_BASE", "https://api.meta.ai/v1"),
                                   api_key=os.environ["MODEL_API_KEY"]))
    else:
        print("[meta] MODEL_API_KEY not set, skipped")

    key = os.environ.get("BASETEN_API_KEY", "").strip()
    if not key:
        print("[baseten] BASETEN_API_KEY not set in .env")
        return 1
    bt = OpenAI(base_url=os.environ.get("BASETEN_API_BASE", "https://inference.baseten.co/v1"), api_key=key)
    found = list_models("baseten", bt)
    model = args.model or os.environ.get("MUSE_SPARK_OPEN_MODEL") or next(
        (i for i in found if "muse-spark-open" in i.lower()), None)
    if not model:
        print("\nNo Muse Spark Open id found. Re-run with --model <id> from your Baseten dashboard.")
        return 1
    ok = test_chat_tools(bt, model)
    if ok:
        print(f"\nAll good. Put this in .env to use it:\n    MUSE_SPARK_OPEN_MODEL={model}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
