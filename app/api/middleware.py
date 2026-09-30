"""ASGI middleware: request id, access log and Prometheus HTTP metrics."""

from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.metrics import ApiMetrics

logger = logging.getLogger("app.api.access")

# Polled constantly by the dashboard / probes: keep them out of the INFO log.
_QUIET_ROUTES = {"/health", "/metrics", "/stats", "/documents", "/"}


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, metrics: ApiMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")[:64] or uuid.uuid4().hex
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            method = scope["method"]
            route = scope.get("route")
            # Use the route template, never the raw path: keeps metric label cardinality bounded.
            path = getattr(route, "path", None) or "unmatched"
            if path != "/metrics":
                self.metrics.observe_request(method, path, status, elapsed)
            quiet = method == "GET" and path in _QUIET_ROUTES and status < 400
            level = logging.DEBUG if quiet else (logging.ERROR if status >= 500 else logging.INFO)
            logger.log(
                level,
                "%s %s -> %s",
                method,
                scope["path"],
                status,
                extra={
                    "request_id": request_id,
                    "method": method,
                    "route": path,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
