"""
CloudWatch custom metrics for the Travel Agent (namespace "TravelAgent").

Inside AWS Lambda, metrics are written as CloudWatch Embedded Metric Format (EMF)
log lines: CloudWatch extracts them from the function's log stream, so no extra
IAM permission or API call is needed. Outside Lambda the EMF helpers are no-ops
(the agent process publishes its own metrics with PutMetricData, see agents/).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

NAMESPACE = "TravelAgent"
SERVICE = "FlightTools"
_lock = threading.Lock()


def in_lambda() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def emit(metrics: dict[str, tuple[float, str]], dimensions: dict[str, str],
         dimension_sets: list[list[str]], properties: dict | None = None) -> None:
    """
    Write one EMF record. metrics: {name: (value, unit)}; dimension_sets lists which
    dimension combinations CloudWatch should aggregate on.
    """
    if not in_lambda() or not metrics:
        return
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": dimension_sets,
                "Metrics": [{"Name": n, "Unit": u} for n, (_, u) in metrics.items()],
            }],
        },
        **dimensions,
        **{n: v for n, (v, _) in metrics.items()},
        **(properties or {}),
    }
    line = json.dumps(record, separators=(",", ":"))
    with _lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def tool_call(tool: str, latency_ms: float, *, error: bool, failure: bool, data_source: str) -> None:
    """One tool invocation. error = tool returned {"error": ...}; failure = unhandled exception."""
    emit(
        {
            "ToolInvocations": (1, "Count"),
            "ToolErrors": (1 if error else 0, "Count"),
            "ToolFailures": (1 if failure else 0, "Count"),
            "ToolLatency": (round(latency_ms, 1), "Milliseconds"),
        },
        {"Service": SERVICE, "Tool": tool, "DataSource": data_source},
        [["Service"], ["Service", "Tool"], ["Service", "DataSource"]],
    )


def serpapi_request(latency_ms: float, ok: bool, kind: str) -> None:
    emit(
        {
            "SerpApiRequests": (1, "Count"),
            "SerpApiErrors": (0 if ok else 1, "Count"),
            "SerpApiLatency": (round(latency_ms, 1), "Milliseconds"),
        },
        {"Service": SERVICE, "RequestType": kind},
        [["Service"], ["Service", "RequestType"]],
    )


def serpapi_quota(searches_left: float) -> None:
    emit({"SerpApiSearchesLeft": (searches_left, "Count")}, {"Service": SERVICE}, [["Service"]])


def booking_event(kind: str) -> None:
    """kind: BookingsCreated | BookingsCancelled | BookingFailures"""
    emit({kind: (1, "Count")}, {"Service": SERVICE}, [["Service"]])
