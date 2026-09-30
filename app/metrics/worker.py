"""Prometheus metrics for a worker process (served on WORKER_METRICS_PORT)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram


class WorkerMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.jobs = Counter(
            "vectorpipe_worker_jobs_total",
            "Messages handled by this worker, by outcome.",
            ["outcome"],
            registry=self.registry,
        )
        self.failures = Counter(
            "vectorpipe_worker_job_failures_total",
            "Processing attempts that raised an error in this worker.",
            registry=self.registry,
        )
        self.duration = Histogram(
            "vectorpipe_worker_job_duration_seconds",
            "Wall-clock time spent processing one message.",
            registry=self.registry,
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
        )
