"""Prometheus metrics for the web app: requests, latency and errors per route, and API calls per key.

Scraped by Prometheus (k8s/monitoring/podmonitor-web.yaml) straight from the pod at /metrics. Requests that came
through the proxy (they carry X-Forwarded-For) get a 404, so the endpoint isn't public.
Gunicorn runs several workers, so this uses prometheus_client's multiprocess mode (PROMETHEUS_MULTIPROC_DIR;
gunicorn.conf.py cleans up after exited workers).
"""
import os
import time

from flask import Response, abort, g, request

MULTIPROC = bool(os.getenv("PROMETHEUS_MULTIPROC_DIR"))

try:
    from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
    from prometheus_client import multiprocess
except ImportError:  # metrics are optional (e.g. a bare dev venv)
    Counter = None

if Counter:
    REQUESTS = Counter("sidelinewire_http_requests_total", "HTTP requests", ["route", "method", "status"])
    LATENCY = Histogram("sidelinewire_http_request_duration_seconds", "Request duration", ["route"],
                        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
    API_CALLS = Counter("sidelinewire_api_requests_total", "JSON API requests by key", ["key", "status"])


def init(app):
    if not Counter:
        return

    @app.before_request
    def _start():
        g._t0 = time.perf_counter()

    @app.after_request
    def _record(response):
        if request.path == "/metrics" or request.path.startswith("/static/"):
            return response
        route = request.url_rule.rule if request.url_rule else "unmatched"
        status = str(response.status_code)
        REQUESTS.labels(route, request.method, status).inc()
        if hasattr(g, "_t0"):
            LATENCY.labels(route).observe(time.perf_counter() - g._t0)
        if request.path.startswith("/api/"):
            API_CALLS.labels(getattr(g, "api_key_name", None) or "none", status).inc()
        return response

    @app.route("/metrics")
    def metrics():
        if request.headers.get("X-Forwarded-For"):
            abort(404)
        if MULTIPROC:
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
        else:
            from prometheus_client import REGISTRY as registry  # noqa: PLC0415
        return Response(generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)
