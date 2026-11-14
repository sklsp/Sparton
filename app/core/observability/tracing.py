"""OpenTelemetry distributed tracing (Ares).

Traces span HTTP requests, job enqueue, and worker execution. Export is
environment-driven and entirely optional:

- OTEL_ENABLED=true + OTEL_EXPORTER_OTLP_ENDPOINT -> OTLP export
- otherwise -> no-op tracer provider; the app behaves identically

Span attributes carry only identifiers (job id, org id, route), never
credentials, tokens, or scraped content.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

from app.core.config import settings
from app.core.observability.logging_config import get_logger

if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer

logger = get_logger(__name__)

_tracer: Tracer | None = None
_configured = False


def configure_tracing() -> bool:
    """Initialize the global tracer provider. Returns True if exporting."""
    global _tracer, _configured
    if _configured:
        return _tracer is not None
    _configured = True

    if not settings.otel_enabled:
        logger.info("OpenTelemetry disabled (OTEL_ENABLED=false)")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.version": _service_version(),
            }
        )
        provider = TracerProvider(resource=resource)
        endpoint = settings.otel_exporter_otlp_endpoint
        if endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
            )
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("sparton")
        logger.info(
            "OpenTelemetry enabled: service=%s endpoint=%s",
            settings.otel_service_name,
            endpoint or "(none, spans local)",
        )
        return True
    except Exception as exc:  # noqa: BLE001 - tracing must never break serving
        logger.warning("OpenTelemetry unavailable (%s); continuing without traces", exc)
        return False


def _service_version() -> str:
    try:
        from app import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return "unknown"


class _NullSpan:
    """No-op stand-in for `opentelemetry.trace.Span` when OTel is unavailable."""

    def __enter__(self) -> _NullSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def record_exception(
        self,
        exception: BaseException,
        attributes: dict[str, Any] | None = None,
        timestamp: int | None = None,
        escaped: bool = False,
    ) -> None:
        pass


class _NullTracer:
    """No-op stand-in for `opentelemetry.trace.Tracer` when OTel is unavailable."""

    def start_as_current_span(self, name: str, **kwargs: Any) -> _NullSpan:
        return _NullSpan()


def get_tracer() -> Tracer | _NullTracer:
    """Return a tracer; a no-op when tracing is disabled/unavailable."""
    global _tracer
    if _tracer is None:
        try:
            from opentelemetry import trace

            _tracer = trace.get_tracer("sparton")
        except Exception:  # noqa: BLE001
            return _NullTracer()
    return _tracer


class _TracedSpan:
    """The context manager returned by `traced`; opens one span on enter."""

    def __init__(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self._name = name
        self._attributes = attributes or {}

    def __enter__(self) -> Span | _NullSpan:
        self._cm = get_tracer().start_as_current_span(self._name)
        span = self._cm.__enter__()
        for key, value in self._attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        return span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return self._cm.__exit__(exc_type, exc, tb)


def traced(name: str, attributes: dict[str, Any] | None = None) -> _TracedSpan:
    """Context-manager span helper safe to use when tracing is disabled."""
    return _TracedSpan(name, attributes)
