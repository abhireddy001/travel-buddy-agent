"""
AWS Lambda target for the Travel Agent's AgentCore Gateway.

The Gateway invokes this function with the tool arguments as the event and the
tool name in context.client_context.custom["bedrockAgentCoreToolName"]
(prefixed "<TargetName>___"). Bookings live in DynamoDB; trend charts are
uploaded to S3 and returned as s3:// URIs. The agent client downloads them
locally - presigned URLs are deliberately NOT returned, because they embed an
AWS access key id that would pass through the model (and is correctly blocked
by the Bedrock Guardrail's AWS_ACCESS_KEY filter).

Environment:
    BOOKINGS_TABLE   DynamoDB table name
    CHARTS_BUCKET    S3 bucket for trend reports
    SERPAPI_SECRET_ID Secrets Manager secret with the SerpApi key (live Google Flights prices)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import boto3

from travel_core import metrics
from travel_core.booking_store import DynamoBookingStore
from travel_core.live_flights import DynamoOfferCache
from travel_core.provider import SimulatedFlightProvider
from travel_core.tools import TOOL_NAMES, TravelTools

log = logging.getLogger()
log.setLevel(logging.INFO)

_s3 = boto3.client("s3")
_BUCKET = os.environ["CHARTS_BUCKET"]


def _publish_chart(_trends: dict, path: Path) -> str:
    key = f"charts/{path.name}"
    _s3.upload_file(str(path), _BUCKET, key,
                    ExtraArgs={"ContentType": "text/html; charset=utf-8"})
    return f"s3://{_BUCKET}/{key}"


TOOLS = TravelTools(
    provider=SimulatedFlightProvider(store=DynamoBookingStore(os.environ["BOOKINGS_TABLE"])),
    chart_publisher=_publish_chart,
    chart_dir=Path("/tmp/charts"),
    offer_cache=DynamoOfferCache(os.environ["BOOKINGS_TABLE"]),
)


def _tool_name(event: dict, context) -> str | None:
    name = None
    cc = getattr(context, "client_context", None)
    if cc is not None:
        name = (getattr(cc, "custom", None) or {}).get("bedrockAgentCoreToolName")
    name = name or event.get("tool_name")          # local testing
    if name and "___" in name:
        name = name.split("___", 1)[1]
    return name


def _is_error(result: str) -> bool:
    """True when the tool returned a top-level {"error": ...} payload."""
    try:
        return "error" in json.loads(result) and len(json.loads(result)) == 1
    except (ValueError, TypeError):
        return False


def lambda_handler(event, context):
    name = _tool_name(event, context)
    if name not in TOOL_NAMES:
        return {"error": f"Unknown tool: {name}"}
    args = {k: v for k, v in (event or {}).items() if not k.startswith("__") and k != "tool_name"}
    log.info("tool=%s args=%s", name, sorted(args))      # arg names only: no PII in logs
    started = time.perf_counter()
    failure = False
    try:
        result = getattr(TOOLS, name)(**args)
    except TypeError as e:                              # bad / missing arguments
        result = json.dumps({"error": f"Invalid arguments for {name}: {e}"})
    except Exception:
        log.exception("tool %s failed", name)
        failure = True
        result = json.dumps({"error": f"{name} failed due to an internal error. Please try again."})
    data_source = "live" if TOOLS.live is not None else "simulated"
    metrics.tool_call(name, (time.perf_counter() - started) * 1000,
                      error=_is_error(result), failure=failure, data_source=data_source)
    return result
