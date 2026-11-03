"""A small, logged tool-calling loop for Meta's models.

Two backends behind one function:
  meta    - Meta Model API, Responses API (recommended by Meta for multi-step tool use:
            it keeps the model's reasoning across tool turns via previous_response_id)
  baseten - Muse Spark Open on Baseten, Chat Completions API (open weights)

Every agent finishes by calling its own "submit_*" tool, so its final output is
structured JSON that the orchestrator can check, not free text it has to parse.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from openai import OpenAI


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., object]
    final: bool = False          # calling it ends the agent's turn


@dataclass
class Backend:
    kind: str                    # "meta" or "baseten"
    model: str
    client: OpenAI

    @property
    def label(self) -> str:
        return f"{self.model} via {'Meta Model API' if self.kind == 'meta' else 'Baseten (open weights)'}"


def backend_from_env(prefix: str = "") -> Backend:
    """prefix "" -> MODEL_API_* (Muse Spark); prefix "JUDGE" may point at Baseten."""
    provider = os.environ.get(f"{prefix}_PROVIDER" if prefix else "PROVIDER", "meta").lower()
    if provider == "baseten":
        return Backend("baseten", os.environ.get("MUSE_SPARK_OPEN_MODEL", "meta-models/muse-spark-open-1026"),
                       OpenAI(base_url=os.environ.get("BASETEN_API_BASE", "https://inference.baseten.co/v1"),
                              api_key=os.environ["BASETEN_API_KEY"]))
    return Backend("meta", os.environ.get("MUSE_SPARK_MODEL", "muse-spark-1.3"),
                   OpenAI(base_url=os.environ.get("MODEL_API_BASE", "https://api.meta.ai/v1"),
                          api_key=os.environ["MODEL_API_KEY"]))


@dataclass
class AgentRun:
    final: dict | None = None
    final_tool: str | None = None
    transcript: list = field(default_factory=list)   # every tool call, for the run log
    tokens: int = 0


def _call_tool(tool: Tool, raw_args: str) -> tuple[dict, object]:
    try:
        args = json.loads(raw_args or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        return {}, {"error": f"invalid arguments: {e}"}
    try:
        return args, tool.fn(**args)
    except Exception as e:                      # tool errors go back to the model, not up the stack
        return args, {"error": f"{type(e).__name__}: {e}"}


def run_agent(backend: Backend, name: str, instructions: str, task: str, tools: list[Tool],
              max_turns: int = 12, on_event: Callable[[str, dict], None] | None = None) -> AgentRun:
    emit = on_event or (lambda kind, data: None)
    by_name = {t.name: t for t in tools}
    run = AgentRun()

    def handle(call_name: str, raw_args: str) -> object:
        tool = by_name.get(call_name)
        if tool is None:
            return {"error": f"no tool named {call_name}"}
        emit("tool_call", {"agent": name, "tool": call_name, "args": raw_args})
        t0 = time.monotonic()
        args, out = _call_tool(tool, raw_args)
        run.transcript.append({"agent": name, "tool": call_name, "args": args, "result": out,
                               "seconds": round(time.monotonic() - t0, 1)})
        emit("tool_result", {"agent": name, "tool": call_name, "result": out})
        if tool.final and not (isinstance(out, dict) and "error" in out):
            run.final, run.final_tool = args, call_name
        return out

    if backend.kind == "meta":
        specs = [{"type": "function", "name": t.name, "description": t.description,
                  "parameters": t.parameters} for t in tools]
        resp = backend.client.responses.create(model=backend.model, instructions=instructions,
                                               input=task, tools=specs)
        for _ in range(max_turns):
            run.tokens += getattr(resp.usage, "total_tokens", 0) or 0
            calls = [o for o in resp.output if o.type == "function_call"]
            if resp.output_text:
                emit("thought", {"agent": name, "text": resp.output_text})
            if not calls:
                if run.final is None:      # model answered in text: remind it to submit
                    resp = backend.client.responses.create(
                        model=backend.model, instructions=instructions, previous_response_id=resp.id, tools=specs,
                        input="Finish by calling your submit tool with your result.")
                    continue
                break
            outputs = [{"type": "function_call_output", "call_id": c.call_id,
                        "output": json.dumps(handle(c.name, c.arguments), default=str)} for c in calls]
            if run.final is not None:
                break
            resp = backend.client.responses.create(model=backend.model, instructions=instructions, previous_response_id=resp.id,
                                                   tools=specs, input=outputs)
        return run

    # Chat Completions (Baseten / open weights)
    specs = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                 "parameters": t.parameters}} for t in tools]
    messages = [{"role": "system", "content": instructions}, {"role": "user", "content": task}]
    for _ in range(max_turns):
        resp = backend.client.chat.completions.create(model=backend.model, messages=messages, tools=specs)
        run.tokens += getattr(resp.usage, "total_tokens", 0) or 0
        msg = resp.choices[0].message
        if msg.content:
            emit("thought", {"agent": name, "text": msg.content})
        if not msg.tool_calls:
            if run.final is None:
                messages += [{"role": "assistant", "content": msg.content or ""},
                             {"role": "user", "content": "Finish by calling your submit tool with your result."}]
                continue
            break
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                        for c in msg.tool_calls]})
        for c in msg.tool_calls:
            out = handle(c.function.name, c.function.arguments)
            messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(out, default=str)})
        if run.final is not None:
            break
    return run
