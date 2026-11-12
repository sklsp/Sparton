"""Tests for app.core.observability.tracing: optional OpenTelemetry spans."""

from __future__ import annotations

import builtins

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import app
from app.core.config import settings
from app.core.observability import tracing


@pytest.fixture(autouse=True)
def _reset_tracing_globals(monkeypatch):
    """Every test starts from the module's initial state: no tracer, unconfigured."""
    monkeypatch.setattr(tracing, "_tracer", None)
    monkeypatch.setattr(tracing, "_configured", False)


@pytest.fixture()
def otlp_capture(monkeypatch):
    """Route OTLP export to an in-memory exporter; nothing leaves the process.

    configure_tracing builds its own TracerProvider and registers it as the
    global one. We intercept set_tracer_provider / get_tracer so that provider
    stays local to this test: no global OpenTelemetry state is touched, and a
    later test in the same process cannot be poisoned by an earlier configure.
    """
    exporters: list[InMemorySpanExporter] = []
    endpoints: list[str | None] = []
    providers: list = []

    def fake_otlp_exporter(*_args, **kwargs):
        exporter = InMemorySpanExporter()
        exporters.append(exporter)
        endpoints.append(kwargs.get("endpoint"))
        return exporter

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        fake_otlp_exporter,
    )
    monkeypatch.setattr(otel_trace, "set_tracer_provider", providers.append)

    def fake_get_tracer(name):
        return providers[-1].get_tracer(name)

    monkeypatch.setattr(otel_trace, "get_tracer", fake_get_tracer)

    yield {"exporters": exporters, "endpoints": endpoints, "providers": providers}

    for provider in providers:
        provider.shutdown()


class TestConfigureDisabled:
    def test_returns_false_and_keeps_tracer_none(self, monkeypatch):
        monkeypatch.setattr(settings, "otel_enabled", False)

        assert tracing.configure_tracing() is False
        assert tracing._configured is True
        assert tracing._tracer is None

    def test_second_call_hits_the_cached_early_return(self, monkeypatch):
        monkeypatch.setattr(settings, "otel_enabled", False)

        assert tracing.configure_tracing() is False
        # Already configured: the early return reports the (absent) tracer.
        assert tracing.configure_tracing() is False


class TestConfigureEnabled:
    def test_with_endpoint_exports_spans_to_the_otlp_path(self, monkeypatch, otlp_capture):
        monkeypatch.setattr(settings, "otel_enabled", True)
        monkeypatch.setattr(settings, "otel_exporter_otlp_endpoint", "http://collector.local:4318")

        assert tracing.configure_tracing() is True
        assert tracing._tracer is not None
        # The exporter was pointed at the OTLP traces path derived from the endpoint.
        assert otlp_capture["endpoints"] == ["http://collector.local:4318/v1/traces"]

        with tracing.traced("job.run", {"job.id": "j-1", "org.id": 7, "skipped": None}) as span:
            span.set_attribute("extra", "value")

        otlp_capture["providers"][0].force_flush()
        spans = otlp_capture["exporters"][0].get_finished_spans()
        assert [s.name for s in spans] == ["job.run"]
        attrs = spans[0].attributes
        assert attrs["job.id"] == "j-1"
        assert attrs["org.id"] == 7
        assert attrs["extra"] == "value"
        # None-valued attributes are dropped by the helper, never sent.
        assert "skipped" not in attrs

    def test_without_endpoint_keeps_spans_local(self, monkeypatch, otlp_capture):
        monkeypatch.setattr(settings, "otel_enabled", True)
        monkeypatch.setattr(settings, "otel_exporter_otlp_endpoint", None)

        assert tracing.configure_tracing() is True
        # No endpoint -> no exporter constructed at all.
        assert otlp_capture["exporters"] == []
        with tracing.traced("local.op"):  # still works; spans stay in-process
            pass

    def test_second_call_returns_cached_state(self, monkeypatch, otlp_capture):
        monkeypatch.setattr(settings, "otel_enabled", True)

        assert tracing.configure_tracing() is True
        # Early return: already configured, so it reports the (present) tracer.
        assert tracing.configure_tracing() is True

    def test_sdk_failure_falls_back_to_no_traces(self, monkeypatch):
        monkeypatch.setattr(settings, "otel_enabled", True)

        def boom(*_args, **_kwargs):
            raise RuntimeError("collector unreachable")

        monkeypatch.setattr("opentelemetry.sdk.trace.TracerProvider", boom)

        assert tracing.configure_tracing() is False
        assert tracing._tracer is None


class TestServiceVersion:
    def test_reads_the_app_version(self):
        assert tracing._service_version() == app.__version__

    def test_falls_back_to_unknown_when_missing(self, monkeypatch):
        monkeypatch.delattr(app, "__version__")
        assert tracing._service_version() == "unknown"


class TestGetTracer:
    def test_caches_the_opentelemetry_tracer(self):
        first = tracing.get_tracer()
        second = tracing.get_tracer()
        assert first is second
        assert tracing._tracer is first

    def test_null_tracer_when_opentelemetry_is_missing(self, monkeypatch):
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "opentelemetry":
                raise ImportError("no OpenTelemetry in this environment")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)

        tracer = tracing.get_tracer()
        # The null fallback is not cached: the module global stays None.
        assert tracing._tracer is None
        with tracing.traced("still.works") as span:
            span.set_attribute("k", "v")  # no-op, must not raise
            span.record_exception(ValueError("x"))  # same surface as a real span
        assert tracer.start_as_current_span("again") is not None


class TestTracedHelper:
    def test_no_op_when_tracing_is_disabled(self):
        with tracing.traced("job.run", {"job.id": "j-9"}) as span:
            assert span is not None
            span.set_attribute("k", "v")  # must not raise without a real tracer

    def test_exception_propagates_and_span_closes(self):
        with pytest.raises(ValueError, match="boom"):
            with tracing.traced("failing.op"):
                raise ValueError("boom")

