from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter


class SafeExporter(SpanExporter):
    def __init__(self, exporter): self.exporter = exporter

    def export(self, spans):
        try:
            return self.exporter.export(spans)
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self):
        try: self.exporter.shutdown()
        except Exception: pass


def create_provider(settings):
    provider = TracerProvider(resource=Resource.create({'service.name': settings.otel_service_name}),
        sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)))
    provider.add_span_processor(BatchSpanProcessor(SafeExporter(OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint, timeout=2)), max_queue_size=1024,
        schedule_delay_millis=500, max_export_batch_size=128))
    return provider
