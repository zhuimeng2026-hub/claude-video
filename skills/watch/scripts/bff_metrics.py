"""In-process Prometheus metrics for the BFF.

Phase 3.2 — no external dep (`prometheus_client`). Implements the
subset of metrics we actually emit: counters, gauges, and a single
histogram (manual bucketing). Sufficient for MVP / homelab ops.

Output format is the Prometheus 0.0.4 text exposition format — drop-in
compatible with `prometheus` and `victoria-metrics`. See
https://prometheus.io/docs/instrumenting/exposition_formats/.

Why no prometheus_client:
  - Adds a transitive dep tree (multiprocess, asgiref, ...)
  - Default metric registration is global / hidden (registry side
    effects at import time)
  - Our metric surface is tiny (5 counters, 1 gauge, 1 histogram);
    a 100-line hand-roll is more honest than 100KB of library code

If we ever need richer histogram semantics (exemplars, native
histograms), swap in prometheus_client — the wire format is the
same so scrapers don't notice.

Thread safety: simple `threading.Lock` around dict reads/writes.
Volume is low (one increment per tool call); contention is fine.
"""
from __future__ import annotations

import threading
import time
from typing import Iterable


class _Counter:
    def __init__(self, name: str, help: str, labelnames: tuple[str, ...] = ()):
        self.name = name
        self.help = help
        self.labelnames = labelnames
        # values[(label_values,)] -> int
        self._values: dict[tuple[str, ...], int] = {}
        self._lock = threading.Lock()

    def inc(self, amount: int = 1, **labels: str) -> None:
        key = tuple(labels.get(n, "") for n in self.labelnames)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def render(self) -> Iterable[str]:
        with self._lock:
            values = dict(self._values)
        if not values:
            yield f"# HELP {self.name} {self.help}"
            yield f"# TYPE {self.name} counter"
            return
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} counter"
        for key, val in sorted(values.items()):
            labels_str = ""
            if self.labelnames:
                pairs = ",".join(f'{n}="{v}"' for n, v in zip(self.labelnames, key))
                labels_str = "{" + pairs + "}"
            yield f"{self.name}{labels_str} {val}"


class _Gauge:
    def __init__(self, name: str, help: str, labelnames: tuple[str, ...] = ()):
        self.name = name
        self.help = help
        self.labelnames = labelnames
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        key = tuple(labels.get(n, "") for n in self.labelnames)
        with self._lock:
            self._values[key] = value

    def render(self) -> Iterable[str]:
        with self._lock:
            values = dict(self._values)
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} gauge"
        for key, val in sorted(values.items()):
            labels_str = ""
            if self.labelnames:
                pairs = ",".join(f'{n}="{v}"' for n, v in zip(self.labelnames, key))
                labels_str = "{" + pairs + "}"
            yield f"{self.name}{labels_str} {val}"


class _Histogram:
    """Manual exponential-bucket histogram. Buckets are inclusive
    upper bounds in seconds. Default buckets cover 5ms → 10s.
    """
    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

    def __init__(self, name: str, help: str, labelnames: tuple[str, ...] = (),
                 buckets: tuple[float, ...] = DEFAULT_BUCKETS):
        self.name = name
        self.help = help
        self.labelnames = labelnames
        self.buckets = buckets
        # values[(label_values,)] -> {bucket: count, "+Inf": count, sum: float}
        self._values: dict[tuple[str, ...], dict[str, float]] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(labels.get(n, "") for n in self.labelnames)
        with self._lock:
            if key not in self._values:
                self._values[key] = {b: 0 for b in self.buckets}
                self._values[key]["+Inf"] = 0
                self._values[key]["_sum"] = 0.0
            counts = self._values[key]
            for b in self.buckets:
                if value <= b:
                    counts[b] += 1
            counts["+Inf"] += 1
            counts["_sum"] += value

    def render(self) -> Iterable[str]:
        """Render in Prometheus 0.0.4 format.

        Each observation is stored in EVERY bucket it's <=
        (see `observe`). So `counts[b]` already IS the cumulative
        count of observations <= b — we output it directly without
        summing (the earlier `cumulative += counts[b]` was double-
        counting). Same for the +Inf bucket.
        """
        with self._lock:
            values = dict(self._values)
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} histogram"
        for key, counts in sorted(values.items()):
            labels_base = ""
            if self.labelnames:
                pairs = ",".join(f'{n}="{v}"' for n, v in zip(self.labelnames, key))
                labels_base = "{" + pairs + ","
            for b in self.buckets:
                yield f'{self.name}_bucket{labels_base}le="{b}"}} {int(counts[b])}'
            yield f'{self.name}_bucket{labels_base}le="+Inf"}} {int(counts["+Inf"])}'
            yield f'{self.name}_sum{labels_base}}} {counts["_sum"]:.6f}'
            yield f'{self.name}_count{labels_base}}} {int(counts["+Inf"])}'


# ─── Registry — module-level singletons ──────────────────────────────────


# Counters
REQUESTS_TOTAL = _Counter(
    "watch_bff_requests_total",
    "Total HTTP requests by tool/status",
    labelnames=("tool", "status"),
)

# Gauges
ACTIVE_WATCHES = _Gauge(
    "watch_bff_active_watches",
    "Number of /api/watch start events that have not reached a terminal state",
)
MCP_CONNECTED = _Gauge(
    "watch_bff_mcp_connected",
    "1 if MCP session is initialized, 0 otherwise",
)
POOL_SIZE_USED = _Gauge(
    "watch_bff_pool_size_used",
    "Number of MCP subprocess slots currently active (Phase 3.3)",
)

# Histogram
TOOL_DURATION = _Histogram(
    "watch_bff_tool_duration_seconds",
    "Wall time for MCP tool calls (proxy + handler)",
    labelnames=("tool",),
)


def time_block(name: str):
    """Context manager: time an operation and increment the
    histogram under `name`. Usage:
        with time_block("watch"):
            ... call state.call_tool(...)
    """
    return _Timer(name)


class _Timer:
    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.perf_counter() - self.start
        TOOL_DURATION.observe(elapsed, tool=self.name)


def render() -> str:
    """Render all metrics in Prometheus 0.0.4 text exposition format."""
    lines: list[str] = []
    for metric in (REQUESTS_TOTAL, ACTIVE_WATCHES, MCP_CONNECTED, TOOL_DURATION):
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"
