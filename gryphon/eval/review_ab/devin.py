"""Devin CLI backend for the A/B review benchmark.

The claude backend drives ``claude -p``; this one drives ``devin -p``. The
pieces map differently:

- The review prompt goes through ``--prompt-file``; the printed answer on
  stdout holds the findings JSON block (Devin has no ``--json-schema``).
- The gryphon MCP is configured per clone in ``.devin/mcp_config.json`` and
  permissions in ``.devin/config.json`` — which also lets the harness forbid
  the ``edit`` tool outright, so reviews are read-only by construction.
- ``--export`` writes an ATIF transcript; ``parse_atif`` reads tool calls,
  per-request token usage when the export carries it, and estimates context
  size from message bytes otherwise. When the user's devin config enables an
  OTLP endpoint, ``OtelSink`` additionally captures real ``api_request``
  token counts and ``tool_result`` byte sizes.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .sandbox import ArmSandbox

logger = logging.getLogger(__name__)

# Devin tool names -> the claude-ish names audit_tool_calls understands.
TOOL_NAME_MAP = {
    "exec": "Bash",
    "read": "Read",
    "grep": "Grep",
    "glob": "Glob",
    "edit": "Edit",
    "write": "Write",
}


def devin_config(arm: str) -> dict:
    """``.devin/config.json`` permissions for one arm.

    Read tools and shell access are allowed; the review must not edit files
    and must not reach the network or the graph CLI — the same contract the
    claude backend expresses through allowedTools/disallowedTools.
    """
    allow = ["read", "grep", "glob", "exec"]
    if arm != "baseline":
        allow.append("mcp__gryphon__*")
    deny = [
        "edit",
        "Fetch(*)",
        "Exec(rm)", "Exec(git push)", "Exec(git pull)", "Exec(git fetch)",
        "Exec(git clone)", "Exec(git remote)", "Exec(git ls-remote)",
        "Exec(gh)", "Exec(curl)", "Exec(wget)", "Exec(iwr)",
        "Exec(gryphon)", "Exec(uv)", "Exec(uvx)",
        "Exec(python -m gryphon)", "Exec(python3 -m gryphon)",
        "Exec(py -m gryphon)", "Exec(pip)", "Exec(pip3)",
    ]
    return {"permissions": {"allow": allow, "deny": deny}}


def devin_mcp_config(sandbox: ArmSandbox, python: str | None = None) -> dict:
    """``.devin/mcp_config.json`` for a graph arm."""
    return {
        "mcpServers": {
            "gryphon": {
                "command": python or sys.executable,
                "args": ["-m", "gryphon", "serve", "--repo", str(sandbox.repo)],
                "env": sandbox.gryphon_env(),
            }
        }
    }


def write_devin_config(sandbox: ArmSandbox, arm: str, python: str | None = None) -> None:
    """Write the arm's ``.devin`` config into the clone.

    The files are untracked and per-kind (baseline and graph clones are
    separate), so they do not need restoring between arms or reps.
    """
    dotdevin = sandbox.repo / ".devin"
    dotdevin.mkdir(exist_ok=True)
    (dotdevin / "config.json").write_text(
        json.dumps(devin_config(arm), indent=2), encoding="utf-8",
    )
    mcp_path = dotdevin / "mcp_config.json"
    if arm != "baseline":
        mcp_path.write_text(
            json.dumps(devin_mcp_config(sandbox, python), indent=2), encoding="utf-8",
        )
    elif mcp_path.exists():
        mcp_path.unlink()


def build_devin_command(
    sandbox: ArmSandbox,
    model: str,
    *,
    prompt_file: Path,
    export_path: Path,
    devin_bin: str = "devin",
    otel_endpoint: str | None = None,
) -> list[str]:
    """The ``devin -p`` argv for one arm."""
    cmd = [
        devin_bin, "-p",
        "--model", model,
        # "auto" auto-approves read-only tools; the arm's .devin/config.json
        # allow/deny rules still decide exec commands and MCP tools.
        "--permission-mode", "auto",
        "--respect-workspace-trust", "false",
        "--prompt-file", str(prompt_file),
        "--export", str(export_path),
    ]
    if otel_endpoint:
        # A dedicated config file keeps the OTLP endpoint out of the user's
        # global config. ``--config`` points Devin at it for this run only.
        cmd += ["--config", str(_otel_config(export_path, otel_endpoint))]
    return cmd


def _otel_config(export_path: Path, endpoint: str) -> Path:
    """A minimal devin config enabling OTLP export to *endpoint*."""
    path = export_path.with_suffix(".devin-config.json")
    path.write_text(
        json.dumps({"otel": {"endpoint": endpoint, "enabled": True}}, indent=2),
        encoding="utf-8",
    )
    return path


# --- ATIF parsing -------------------------------------------------------------


def _walk(node: Any):
    """Yield every dict nested inside *node*."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


_TOKEN_KEYS = {
    "input_tokens": "input_tokens",
    "input": "input_tokens",
    "prompt_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "output": "output_tokens",
    "completion_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cacheRead": "cache_read_input_tokens",
    "cache_read_input_tokens": "cache_read_input_tokens",
    "cache_creation_tokens": "cache_creation_input_tokens",
    "cacheCreation": "cache_creation_input_tokens",
    "cache_creation_input_tokens": "cache_creation_input_tokens",
}


def _text_of(content: Any) -> str:
    """Best-effort plain text of a message/step content field."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content
        )
    return str(content)


def parse_atif(path: Path) -> dict:
    """Parse a Devin ``--export`` ATIF transcript.

    Returns the same shape ``parse_stream`` produces for claude so
    ``summarize``/``audit_tool_calls`` work unchanged: tool calls normalized
    to Bash/Read/Grep/Glob names, per-request usage rows (if the export
    carries them) and a synthesized ``result`` event whose ``usage`` sums the
    observed tokens — or ``None`` when the export has none, in which case
    context sizes are estimated from message bytes (~4 chars/token).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not parse ATIF export %s: %s", path, exc)
        return {"init": {}, "result": None, "tool_calls": [],
                "hook_events": [], "assistant_usage": []}

    tool_calls: list[dict] = []
    usage_rows: list[dict] = []
    assistant_texts: list[str] = []
    tool_result_bytes = 0
    num_turns = 0

    for node in _walk(data):
        # Tool calls: ATIF steps carry {"tool_calls": [{"function_name": ...,
        # "arguments": {...}}]}; other exports use "name"/"function.name".
        for call in node.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = (
                call.get("name")
                or call.get("function_name")
                or (call.get("function") or {}).get("name")
            )
            args = call.get("arguments") or call.get("input") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"command": args} if name == "exec" else {"raw": args}
            if name:
                tool_calls.append({"name": TOOL_NAME_MAP.get(name, name), "input": args})
        # Steps of type tool_call also appear as standalone nodes in some exports.
        if node.get("type") in ("tool_call", "tool_use") and node.get("tool_name"):
            name = node["tool_name"]
            tool_calls.append({
                "name": TOOL_NAME_MAP.get(name, name),
                "input": node.get("arguments") or node.get("input") or {},
            })
        # Per-request usage. ATIF `metrics` reports prompt_tokens as the full
        # prompt including the cached prefix; claude usage reports fresh input
        # separately from cache_read. Normalize to the claude shape so context
        # sizes are comparable across backends.
        usage = node.get("usage") or node.get("metrics") or {}
        if isinstance(usage, dict) and "prompt_tokens" in usage:
            prompt = int(usage.get("prompt_tokens") or 0)
            cached = int(usage.get("cached_tokens") or 0)
            extra = usage.get("extra") if isinstance(usage.get("extra"), dict) else {}
            creation = int(extra.get("cache_creation_input_tokens") or 0)
            usage_rows.append({
                # ATIF prompt_tokens is the whole window (fresh + cache_read
                # + cache_creation); fresh input is what remains.
                "input_tokens": max(prompt - cached - creation, 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": creation,
            })
        elif isinstance(usage, dict):
            row = {}
            for key, canonical in _TOKEN_KEYS.items():
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    row[canonical] = int(value)
            if row:
                usage_rows.append(row)
        # Assistant steps: ATIF marks them source="agent". A step is a turn
        # even when message is empty (pure tool-call turn).
        role = node.get("role") or node.get("source")
        if role == "agent" or role == "assistant":
            msg = node.get("message")
            text = _text_of(
                msg.get("content") if isinstance(msg, dict)
                else msg if msg is not None else node.get("content")
            )
            if "step_id" in node or "timestamp" in node:
                num_turns += 1
            if text:
                assistant_texts.append(text)
                if num_turns == 0:
                    num_turns = 1
        # Tool result sizes: explicit byte counts when present, otherwise the
        # length of the observation contents ATIF records per call.
        size = node.get("tool_result_size_bytes") or node.get("result_size_bytes")
        if isinstance(size, (int, float)):
            tool_result_bytes += int(size)
        obs = node.get("observation")
        if isinstance(obs, dict):
            for res in obs.get("results") or []:
                if isinstance(res, dict):
                    tool_result_bytes += len(str(res.get("content") or ""))

    findings = _extract_findings("\n".join(assistant_texts[-3:]))

    if not usage_rows and isinstance(data, dict):
        # The export aggregates usage in `final_metrics` rather than per
        # request. total_prompt_tokens includes the cached prefix, so split
        # it the way claude usage does: fresh input + cache_read.
        fm = data.get("final_metrics")
        if isinstance(fm, dict):
            prompt = fm.get("total_prompt_tokens") or 0
            cached = fm.get("total_cached_tokens") or 0
            completion = fm.get("total_completion_tokens") or 0
            if prompt or completion:
                usage_rows.append({
                    "input_tokens": int(max(prompt - cached, 0)),
                    "output_tokens": int(completion),
                    "cache_read_input_tokens": int(cached),
                    "cache_creation_input_tokens": 0,
                })
                if not num_turns:
                    num_turns = int(fm.get("total_steps") or 0)

    if usage_rows:
        total = {
            k: sum(r.get(k, 0) for r in usage_rows)
            for k in ("input_tokens", "output_tokens",
                      "cache_read_input_tokens", "cache_creation_input_tokens")
        }
    else:
        # No provider numbers: estimate the context that actually entered the
        # window from transcript bytes (≈4 chars/token), a comparison metric
        # valid between arms of the same backend.
        total = None

    # The run itself succeeded even when it found nothing — the summarize
    # contract marks missing findings as "no_structured_output", not error.
    result = {
        "subtype": "success",
        "is_error": False,
        "num_turns": num_turns or None,
        "usage": total,
        "structured_output": findings,
        "result": assistant_texts[-1][:500] if assistant_texts else None,
    }
    return {
        "init": {"tools": sorted({c["name"] for c in tool_calls})},
        "result": result,
        "tool_calls": tool_calls,
        "hook_events": [],
        "assistant_usage": usage_rows,
        "tool_result_bytes": tool_result_bytes,
        "final_text": assistant_texts[-1] if assistant_texts else "",
    }


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def _extract_findings(text: str) -> dict | None:
    """The ``{"summary": ..., "findings": [...]}`` object from the printed answer."""
    for match in reversed(list(_JSON_BLOCK_RE.finditer(text))):
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "findings" in obj:
            return {"summary": obj.get("summary", ""), "findings": obj["findings"]}
    # Last resort: the whole tail is a bare JSON object.
    tail = text[text.rfind("{"):]
    try:
        obj = json.loads(tail)
        if isinstance(obj, dict) and "findings" in obj:
            return {"summary": obj.get("summary", ""), "findings": obj["findings"]}
    except json.JSONDecodeError:
        pass
    return None


# --- OTLP capture ------------------------------------------------------------


class OtelSink:
    """A localhost OTLP/HTTP-JSON collector writing every POST to a file.

    Devin exports ``api_request`` log records (input/output/cache tokens per
    model request) and ``devin.token.usage`` metrics when its config has an
    ``otel.endpoint``. Environment variables can point the export at this
    collector but cannot activate it on their own: the devin config (here a
    dedicated ``--config`` file) must carry the endpoint.
    """

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str | None:
        if self._server is None:
            return None
        return f"http://localhost:{self._server.server_address[1]}"

    def __enter__(self) -> "OtelSink":
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                with open(sink.out_path, "ab") as f:
                    f.write(self.path.encode() + b" " + body + b"\n")
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def parse(self) -> dict:
        """Aggregate token usage and tool results from the captured payloads."""
        usage = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }
        requests: list[dict] = []
        tool_results: list[dict] = []
        if not self.out_path.exists():
            return {"usage": None, "requests": [], "tool_results": []}
        for line in self.out_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            path, _, raw = line.partition(" ")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if "/v1/logs" not in path:
                continue
            for record in _walk(payload):
                if record.get("event.name") != "api_request":
                    continue
                attrs = record.get("attributes") or record
                row = {
                    "input_tokens": int(attrs.get("input_tokens") or 0),
                    "output_tokens": int(attrs.get("output_tokens") or 0),
                    "cache_read_input_tokens": int(
                        attrs.get("cache_read_tokens") or 0
                    ),
                    "cache_creation_input_tokens": int(
                        attrs.get("cache_creation_tokens") or 0
                    ),
                }
                requests.append(row)
                for k in usage:
                    usage[k] += row[k]
        return {
            "usage": usage if requests else None,
            "requests": requests,
            "tool_results": tool_results,
        }
