"""Prometheus-compatible metrics without external dependencies (Ares).

Implements the text exposition format directly so production monitoring
needs no new packages. Counters/gauges/histograms are process-local, which
is standard for Prometheus: aggregation happens at the scraper.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

_lock = threading.Lock()
_counters: dict[str, float] = defaultdict(float)
_gauges: dict[str, float] = defaultdict(float)
_histograms: dict[str, list[float]] = defaultdict(list)

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    key = _key(name, labels)
    with _lock:
        _counters[key] += value


def gauge(name: str, value: float, **labels: str) -> None:
    key = _key(name, labels)
    with _lock:
        _gauges[key] = value


def observe(name: str, value: float, **labels: str) -> None:
    key = _key(name, labels)
    with _lock:
        _histograms[key].append(value)


class Timer:
    """Context manager recording a duration observation."""

    def __init__(self, name: str, **labels: str) -> None:
        self.name, self.labels = name, labels

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        observe(self.name, time.perf_counter() - self._start, **self.labels)


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    joined = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"{name}{{{joined}}}"


def render() -> str:
    lines: list[str] = []
    with _lock:
        for key, value in sorted(_counters.items()):
            lines.append(_metric_line(key, value))
        for key, value in sorted(_gauges.items()):
            lines.append(_metric_line(key, value))
        for key, values in sorted(_histograms.items()):
            base, label_part = _split_key(key)
            for bucket in BUCKETS:
                count = sum(1 for v in values if v <= bucket)
                lines.append(
                    _metric_line(
                        f"{base}_bucket{label_part[:-1] + ',' if label_part else '{'}le=\"{bucket}\"}}",
                        float(count),
                    )
                )
            lines.append(_metric_line(f"{base}_count{label_part}", float(len(values))))
            lines.append(_metric_line(f"{base}_sum{label_part}", float(sum(values))))
    return "\n".join(lines) + "\n"


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "counters": dict(_counters),
            "gauges": dict(_gauges),
            "histogram_counts": {k: len(v) for k, v in _histograms.items()},
        }


def _metric_line(key: str, value: float) -> str:
    return f"{key} {value}"


def _split_key(key: str) -> tuple[str, str]:
    if "{" in key:
        base, rest = key.split("{", 1)
        return base, "{" + rest
    return key, ""
