#!/usr/bin/env python3
"""Step 1: prove the Meta Model API key works, including tool calling.

    pip install -r requirements.txt
    python agents/check_api.py

Reads MODEL_API_KEY (and optionally MODEL_API_BASE, MUSE_SPARK_MODEL) from .env.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

key = os.environ.get("MODEL_API_KEY", "").strip()
if not key:
    sys.exit("MODEL_API_KEY is missing. Copy .env.example to .env and paste your key.")

client = OpenAI(base_url=os.environ.get("MODEL_API_BASE", "https://api.meta.ai/v1"), api_key=key)
model = os.environ.get("MUSE_SPARK_MODEL", "muse-spark-1.3")

# 1. Plain call
r = client.responses.create(model=model, input="Reply with exactly: GridTwin online")
print(f"[1] plain call    -> {r.output_text.strip()!r}  ({r.usage.total_tokens} tokens)")

# 2. Tool call: the model should ask to read the battery
tools = [{
    "type": "function",
    "name": "read_battery",
    "description": "Read the emulated battery's cell voltages in millivolts.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}]
r = client.responses.create(
    model=model,
    input="Is any cell above 3650 mV? Use the tool to check.",
    tools=tools,
)
calls = [o for o in r.output if o.type == "function_call"]
if not calls:
    sys.exit(f"[2] tool call     -> FAILED, model answered without the tool: {r.output_text!r}")
print(f"[2] tool call     -> model called {calls[0].name}()")

# 3. Return a fake reading and let the model conclude
r = client.responses.create(
    model=model,
    previous_response_id=r.id,
    tools=tools,
    input=[{"type": "function_call_output", "call_id": calls[0].call_id,
            "output": json.dumps({"cells_mv": [3301, 3304, 3307, 3708]})}],
)
print(f"[3] tool result   -> {r.output_text.strip()}")
print("\nAll good: the key works and Muse Spark can call tools.")
