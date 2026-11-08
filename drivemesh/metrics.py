"""Prometheus metrics. A dedicated registry keeps tests isolated from the global default."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

BYTES_UPLOADED = Counter(
    "dm_bytes_uploaded", "Object bytes written to backends", ["provider"], registry=REGISTRY
)
BYTES_DOWNLOADED = Counter(
    "dm_bytes_downloaded", "Object bytes read from backends", ["provider"], registry=REGISTRY
)
BACKEND_OP_SECONDS = Histogram(
    "dm_backend_op_seconds",
    "Latency of backend operations (including retries)",
    ["op", "provider"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    registry=REGISTRY,
)
BACKEND_RETRIES = Counter(
    "dm_backend_retries", "Retried backend calls", ["provider", "reason"], registry=REGISTRY
)
BACKEND_ERRORS = Counter(
    "dm_backend_errors",
    "Backend calls that failed finally",
    ["provider", "kind"],
    registry=REGISTRY,
)
REPLICAS = Gauge("dm_replicas", "Replicas by state", ["state"], registry=REGISTRY)
UNDER_REPLICATED = Gauge(
    "dm_under_replicated_chunks", "Chunks with fewer healthy replicas than RF", registry=REGISTRY
)
JOBS = Gauge("dm_jobs", "Jobs by state", ["state"], registry=REGISTRY)
REPAIRS = Counter("dm_repairs", "Replica repairs", ["result"], registry=REGISTRY)
SCRUB_ERRORS = Counter("dm_scrub_errors", "Scrubber findings", ["kind"], registry=REGISTRY)
SCRUB_BYTES = Counter("dm_scrub_bytes", "Bytes verified by the scrubber", registry=REGISTRY)
SAFE_MODE = Gauge("dm_safe_mode", "1 while the system is in safe mode", registry=REGISTRY)
FILES_COMMITTED = Counter("dm_files_committed", "Committed file versions", registry=REGISTRY)
