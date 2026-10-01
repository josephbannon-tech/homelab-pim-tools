"""Optional OpenTelemetry tracing.

Enabled only when OTEL_EXPORTER_OTLP_ENDPOINT is set (the SDK's own standard
variable), so tests and local runs emit nothing and need no collector. Each
HTTP request becomes a server span; the CalDAV calls inside it are not yet
child spans (the `caldav` client uses niquests, which is not instrumented
here), so the span shows tool latency end to end, which is the number that
matters for the chat door.
"""

from __future__ import annotations

import os

from fastapi import FastAPI


def configure(app: FastAPI) -> bool:
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from . import __version__

    resource = Resource.create(
        {
            SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME", "pim-tools"),
            SERVICE_VERSION: __version__,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # /health is probed every few seconds by kubelet and blackbox; keep it out.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
    return True
