"""Prometheus metrics for the API process."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from app.models import DocumentStatus

logger = logging.getLogger(__name__)


class ApiMetrics:
    """Per-app metric objects registered in a private registry (no global state, test friendly)."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.registry = registry
        self.http_requests = Counter(
            "vectorpipe_http_requests_total",
            "HTTP requests handled.",
            ["method", "path", "status"],
            registry=registry,
        )
        self.http_errors = Counter(
            "vectorpipe_http_errors_total",
            "HTTP responses with status >= 400.",
            ["method", "path", "status"],
            registry=registry,
        )
        self.http_latency = Histogram(
            "vectorpipe_http_request_duration_seconds",
            "HTTP request latency.",
            ["method", "path"],
            registry=registry,
        )
        self.documents_submitted = Counter(
            "vectorpipe_documents_submitted_total",
            "Documents accepted by POST /documents.",
            registry=registry,
        )

    def observe_request(self, method: str, path: str, status: int, seconds: float) -> None:
        self.http_requests.labels(method, path, str(status)).inc()
        self.http_latency.labels(method, path).observe(seconds)
        if status >= 400:
            self.http_errors.labels(method, path, str(status)).inc()


class StateCollector(Collector):
    """Metrics that live in shared state (database / queue) and are computed at scrape time.

    Because the worker is a separate process, its failures are exposed here via the database, which
    makes ``vectorpipe_processing_failures_total`` correct across restarts and multiple workers.
    """

    def __init__(self, snapshot: Callable[[], dict]) -> None:
        self._snapshot = snapshot

    def collect(self) -> Iterable:
        up = GaugeMetricFamily("vectorpipe_database_up", "1 if the database answered the scrape.")
        try:
            snap = self._snapshot()
        except Exception:  # noqa: BLE001 - a scrape must never fail because a dependency is down
            logger.exception("metrics scrape could not read application state")
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up

        docs = GaugeMetricFamily("vectorpipe_documents", "Documents by current status.", labels=["status"])
        for status in DocumentStatus:
            docs.add_metric([status.value], snap["counts"][status])
        yield docs

        failures = CounterMetricFamily(
            "vectorpipe_processing_failures",
            "Processing attempts that failed (derived from the database, spans all workers).",
        )
        failures.add_metric([], snap["processing_failures"])
        yield failures

        queue = GaugeMetricFamily("vectorpipe_queue_messages", "Queue messages by state.", labels=["state"])
        for state in ("visible", "in_flight", "dead"):
            queue.add_metric([state], snap["queue"][state])
        yield queue
