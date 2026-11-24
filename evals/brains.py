"""Brain adapters: the thing under test, behind one method.

`ask(messages, tools_url) -> reply text`. The brain reaches the eval server
however it does in production; the harness only hands it the URL.

  ShimBrain      an OpenAI-shaped endpoint that runs its own tool loop over
                 MCP (headless Claude Code behind a small shim). The harness
                 starts a private copy of the shim pointed at the eval server.
  ToolLoopBrain  a plain OpenAI-compatible model endpoint (llama.cpp and
                 friends). The harness advertises the five operations as
                 functions and runs the loop itself over the REST door.
  OracleBrain    no model: performs each case's expected calls. Proves in CI
                 that every case is satisfiable and the scorer agrees.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx


class Brain(Protocol):
    name: str

    def ask(self, messages: list[dict[str, str]], case: dict[str, Any]) -> str: ...


def system_prompt(tz: ZoneInfo) -> str:
    """What the production front end tells the model each turn. Kept word for
    word in step with the shim, so both brains are given the same brief."""
    now = datetime.now(tz)
    return (
        f"Current date and time: {now:%A %d %B %Y, %H:%M} ({tz.key}, UTC{now:%z}). "
        "Resolve relative dates ('Thursday', 'next week') from this. "
        "Give calendar times as ISO 8601 with this timezone offset. "
        "You have tools for the user's calendar and task lists; use them for "
        "anything about events or lists, and report only what a tool result says. "
        "Never claim an add succeeded unless the tool returned the created object."
    )


# ------------------------------------------------------------------------ shim


class ShimBrain:
    def __init__(self, shim_path: str, tools_url: str, tz: ZoneInfo, timeout: float = 240):
        self.name = "claude-code-pim"
        self.timeout = timeout
        self._tmp = tempfile.TemporaryDirectory(prefix="pim-eval-shim-")
        mcp = os.path.join(self._tmp.name, "mcp.json")
        with open(mcp, "w") as f:
            json.dump({"mcpServers": {"pim": {"type": "http", "url": f"{tools_url}/mcp"}}}, f)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = dict(
            os.environ,
            SHIM_PORT=str(port),
            SHIM_MCP_CONFIG=mcp,
            SHIM_TIMEZONE=tz.key,
            SHIM_WORKDIR=os.path.join(self._tmp.name, "work"),
        )
        self.url = f"http://127.0.0.1:{port}"
        self._proc = subprocess.Popen(
            [sys.executable, shim_path],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        for _ in range(100):
            try:
                httpx.get(f"{self.url}/health")
                return
            except httpx.ConnectError:
                time.sleep(0.1)
        self.close()
        raise RuntimeError(f"shim at {shim_path} did not start")

    def ask(self, messages: list[dict[str, str]], case: dict[str, Any]) -> str:
        r = httpx.post(
            f"{self.url}/v1/chat/completions",
            json={"model": self.name, "messages": messages, "stream": False},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"shim HTTP {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]

    def close(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._tmp.cleanup()


# ------------------------------------------------------------------- tool loop


def _deref(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Inline local $refs so the function schema stands alone."""
    if "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return _deref(node, spec)
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            out[k] = _deref(v, spec)
        elif isinstance(v, list):
            out[k] = [_deref(i, spec) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


def openapi_tools(spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """OpenAPI operations as OpenAI function tools, plus how to call each one."""
    tools, routes = [], {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            name = op.get("operationId")
            if not name:
                continue
            props: dict[str, Any] = {}
            required: list[str] = []
            where: dict[str, str] = {}
            for p in op.get("parameters", []):
                schema = _deref(p.get("schema", {}), spec)
                if p.get("description"):
                    schema.setdefault("description", p["description"])
                props[p["name"]] = schema
                where[p["name"]] = p["in"]
                if p.get("required"):
                    required.append(p["name"])
            body = op.get("requestBody", {}).get("content", {}).get("application/json")
            if body:
                schema = _deref(body["schema"], spec)
                props.update(schema.get("properties", {}))
                required += schema.get("required", [])
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": op.get("description") or op.get("summary", ""),
                        "parameters": {"type": "object", "properties": props, "required": required},
                    },
                }
            )
            routes[name] = {"method": method, "path": path, "where": where}
    return tools, routes


class ToolLoopBrain:
    MAX_ROUNDS = 8

    def __init__(
        self,
        base_url: str,
        model: str,
        tools_url: str,
        tz: ZoneInfo,
        system_extra: str = "",
        timeout: float = 600,
    ):
        self.name = model
        self.system_extra = system_extra
        self.base_url = base_url.rstrip("/")
        self.tools_url = tools_url
        self.tz = tz
        self.http = httpx.Client(timeout=timeout)
        self.tools, self.routes = openapi_tools(httpx.get(f"{tools_url}/openapi.json").json())

    def _call(self, name: str, args: dict[str, Any]) -> str:
        route = self.routes.get(name)
        if route is None:
            return json.dumps({"error": f"no tool named {name}"})
        path, query, body = route["path"], {}, {}
        for k, v in args.items():
            if v is None:
                continue
            where = route["where"].get(k)
            if where == "path":
                path = path.replace("{" + k + "}", str(v))
            elif where == "query":
                query[k] = v
            else:
                body[k] = v
        r = self.http.request(
            route["method"], self.tools_url + path, params=query, json=body or None
        )
        return r.text

    def ask(self, messages: list[dict[str, str]], case: dict[str, Any]) -> str:
        convo: list[dict[str, Any]] = [
            {"role": "system", "content": f"{system_prompt(self.tz)} {self.system_extra}".strip()},
            *messages,
        ]
        for _ in range(self.MAX_ROUNDS):
            r = self.http.post(
                f"{self.base_url}/chat/completions",
                json={"model": self.name, "messages": convo, "tools": self.tools, "temperature": 0},
            )
            if r.status_code != 200:
                raise RuntimeError(f"model HTTP {r.status_code}: {r.text[:200]}")
            msg = r.json()["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content") or ""
            convo.append(
                {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
            )
            for call in calls:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    result = self._call(call["function"]["name"], args)
                except (json.JSONDecodeError, AttributeError) as e:
                    result = json.dumps({"error": f"bad arguments: {e}"})
                convo.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        raise RuntimeError(f"no final answer after {self.MAX_ROUNDS} tool rounds")

    def close(self) -> None:
        self.http.close()


# ---------------------------------------------------------------------- oracle


def _literal(matcher: Any) -> Any:
    if isinstance(matcher, dict):
        if "any" in matcher:
            return _literal(matcher["any"][0])
        if "contains" in matcher:
            return matcher["contains"]
        return None
    return matcher


class OracleBrain:
    """Does what each case expects, over REST. `rogue=True` also writes one
    thing nobody asked for, to prove the bad-write gate fires."""

    def __init__(self, tools_url: str, rogue: bool = False):
        self.name = "rogue" if rogue else "oracle"
        self.url = tools_url
        self.rogue = rogue

    def ask(self, messages: list[dict[str, str]], case: dict[str, Any]) -> str:
        for expect in case["expect"]:
            if expect.get("optional"):
                continue
            args = {k: _literal(v) for k, v in (expect.get("args") or {}).items()}
            tool = expect["tool"]
            if tool == "list_tasks":
                params = {"include_completed": args.get("include_completed", False)}
                httpx.get(f"{self.url}/tasks/{args['list']}", params=params)
            elif tool == "add_task":
                httpx.post(f"{self.url}/tasks", json=args).raise_for_status()
            elif tool == "complete_task":
                listed = httpx.get(f"{self.url}/tasks/{args['list']}").json()["tasks"]
                uid = next(
                    t["uid"] for t in listed if t["title"].casefold() == expect["task"].casefold()
                )
                httpx.post(f"{self.url}/tasks/complete", json={"list": args["list"], "uid": uid})
            elif tool == "list_events":
                at = datetime.fromisoformat(expect["covers"])
                window = {
                    "from": (at - timedelta(hours=1)).isoformat(),
                    "to": (at + timedelta(hours=1)).isoformat(),
                }
                if args.get("calendar"):
                    window["calendar"] = args["calendar"]
                httpx.get(f"{self.url}/events", params=window)
            elif tool == "add_event":
                httpx.post(f"{self.url}/events", json=args).raise_for_status()
            elif tool in ("change_event", "cancel_event"):
                at = datetime.fromisoformat(args["start"])
                window = {
                    "from": (at - timedelta(hours=1)).isoformat(),
                    "to": (at + timedelta(hours=1)).isoformat(),
                    "calendar": "all",
                }
                found = httpx.get(f"{self.url}/events", params=window).json()["events"]
                ev = next(e for e in found if e["title"].casefold() == expect["event"].casefold())
                body = {**args, "uid": ev["uid"], "start": ev["start"], "calendar": ev["calendar"]}
                httpx.post(f"{self.url}/events/{tool.split('_')[0]}", json=body).raise_for_status()
        if self.rogue:
            httpx.post(f"{self.url}/tasks", json={"list": "todo", "title": "Unrequested"})
        checks = case.get("reply") or {}
        return " ".join(checks.get("contains", []) + checks.get("contains_any", [])[:1])

    def close(self) -> None:
        pass
