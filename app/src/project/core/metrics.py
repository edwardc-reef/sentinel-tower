import glob
import os

import prometheus_client
from django.http import HttpResponse
from django_prometheus.exports import ExportToDjangoView
from prometheus_client import REGISTRY, multiprocess

from apps.metagraph.tasks import set_snapshot_health_metrics
from project.celery import CeleryQueueLenCollector


class RecursiveMultiProcessCollector(multiprocess.MultiProcessCollector):
    """A multiprocess collector that scans the directory recursively"""

    def collect(self):
        metrics = {}
        for filename in glob.iglob(os.path.join(self._path, "**/*.db"), recursive=True):
            try:
                file_metrics = self._read_metrics([filename])
            except FileNotFoundError:
                # A service may clear its own stale files while a scrape walks
                # the shared directory. Missing files are transient, not a 500.
                continue
            for name, metric in file_metrics.items():
                if name in metrics:
                    metrics[name].samples.extend(metric.samples)
                else:
                    metrics[name] = metric
        return self._accumulate_metrics(metrics, accumulate=True)


if is_multiprocess := bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR")):
    registry = prometheus_client.CollectorRegistry()
    # The pinned fork's compactor reads pickle data from this writable metrics
    # volume without synchronizing with writers. Keep recursive collection and
    # startup cleanup until the compactor has a safe storage/locking protocol.
    RecursiveMultiProcessCollector(registry)
else:
    registry = REGISTRY

registry.register(CeleryQueueLenCollector())


def metrics_view(request):
    """Exports metrics as a Django view"""

    set_snapshot_health_metrics()

    if is_multiprocess:
        return HttpResponse(
            prometheus_client.generate_latest(registry),
            content_type=prometheus_client.CONTENT_TYPE_LATEST,
        )

    return ExportToDjangoView(request)
