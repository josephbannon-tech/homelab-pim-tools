"""Shared-secret bearer auth, one gate in front of both doors (REST and MCP).

With PIM_TOOLS_TOKEN set, every request except the health probe and the
OpenAPI document must carry `Authorization: Bearer <token>`. Unset, the
server is open, which is the v1 posture (ClusterIP, in-cluster callers only).

This is a shared secret, not identity. It exists so the service can sit on a
NodePort for a caller outside the cluster (an MCP client on another host)
without letting anything else on the LAN write to the calendar. Per-user
identity stays on the roadmap.

Written as pure ASGI rather than Starlette's BaseHTTPMiddleware on purpose:
the MCP transport streams server-sent events, and BaseHTTPMiddleware buffers
response bodies.
"""

from __future__ import annotations

import hmac
import os

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ENV_VAR = "PIM_TOOLS_TOKEN"
# /openapi.json stays open: Open WebUI fetches the spec before it has a reason
# to send a key, and the document holds nothing secret.
EXEMPT_PATHS = frozenset({"/health", "/openapi.json", "/docs", "/redoc"})


class BearerAuth:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        # Read per request so a rotated token needs no code path of its own.
        token = os.environ.get(ENV_VAR)
        if not token:
            await self.app(scope, receive, send)
            return
        presented = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(presented, f"Bearer {token}".encode()):
            response = JSONResponse(
                {"detail": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
