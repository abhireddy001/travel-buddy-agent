"""
Read-only system status for the Travel Agent web app (/status).

Collects, with the instance's AWS credentials, a snapshot of the deployed stack:
AgentCore Gateway + target + tools, Bedrock Guardrail, Bedrock model, Lambda,
DynamoDB, CloudWatch alarms and 24 h metrics. Nothing here can change AWS state:
only Get/Describe/List calls and CloudWatch GetMetricData are used.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_FILE = ROOT / "config" / "deployment.json"
CONFIG_FILE = ROOT / "config" / "agent_config.json"
CACHE_SECONDS = 60
HOURS = 24

_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _iso(v) -> str | None:
    return v.astimezone(timezone.utc).replace(microsecond=0).isoformat() if isinstance(v, datetime) else v


def _section(fn):
    """Run one collector; a failure in one section never breaks the page."""
    try:
        return {"ok": True, **fn()}
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__
        return {"ok": False, "error": code}


def _console(region: str, path: str) -> str:
    return f"https://{region}.console.aws.amazon.com/{path}"


def collect() -> dict:
    with _lock:
        if _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    d = json.loads(DEPLOYMENT_FILE.read_text())
    cfg = json.loads(CONFIG_FILE.read_text())
    region = d["region"]
    model_id = cfg["model"]["model_id"]
    gw_id, g = d["gateway"]["gateway_id"], d["guardrail"]

    def gateway():
        c = boto3.client("bedrock-agentcore-control", region_name=region)
        gw = c.get_gateway(gatewayIdentifier=gw_id)
        targets = []
        for t in c.list_gateway_targets(gatewayIdentifier=gw_id).get("items", []):
            full = c.get_gateway_target(gatewayIdentifier=gw_id, targetId=t["targetId"])
            schema = (((full.get("targetConfiguration") or {}).get("mcp") or {}).get("lambda") or {}).get("toolSchema") or {}
            tools = [x["name"] for x in schema.get("inlinePayload", [])]
            targets.append({"name": full.get("name"), "status": full.get("status"), "tools": tools,
                            "updated": _iso(full.get("updatedAt"))})
        auth = (gw.get("authorizerConfiguration") or {}).get("customJWTAuthorizer") or {}
        return {
            "name": gw.get("name"), "id": gw_id, "status": gw.get("status"), "protocol": gw.get("protocolType"),
            "authorizer": gw.get("authorizerType"), "identity_provider": "Amazon Cognito" if "cognito" in auth.get("discoveryUrl", "") else auth.get("discoveryUrl"),
            "updated": _iso(gw.get("updatedAt")), "targets": targets,
            "console": _console(region, f"bedrock-agentcore/home?region={region}#/gateways"),
        }

    def guardrail():
        b = boto3.client("bedrock", region_name=region)
        gr = b.get_guardrail(guardrailIdentifier=g["guardrail_id"], guardrailVersion=g["guardrail_version"])
        pii = [f"{e['type'].replace('_', ' ').title()} ({e['action'].lower()})"
               for e in (gr.get("sensitiveInformationPolicy") or {}).get("piiEntities", [])]
        policies = [name for key, name in (("contentPolicy", "content filters"), ("topicPolicy", "denied topics"),
                                           ("wordPolicy", "word filters"), ("contextualGroundingPolicy", "grounding"))
                    if gr.get(key)]
        return {"name": gr.get("name"), "id": g["guardrail_id"], "version": g["guardrail_version"],
                "status": gr.get("status"), "blocks": pii, "other_policies": policies or ["none"],
                "updated": _iso(gr.get("updatedAt")),
                "console": _console(region, f"bedrock/home?region={region}#/guardrails")}

    def lambda_fn():
        lam = boto3.client("lambda", region_name=region)
        c = lam.get_function_configuration(FunctionName=d["lambda_arn"])
        return {"name": c["FunctionName"], "state": c.get("State"), "runtime": c.get("Runtime"),
                "architecture": (c.get("Architectures") or ["?"])[0], "memory_mb": c.get("MemorySize"),
                "timeout_s": c.get("Timeout"), "last_modified": c.get("LastModified"),
                "last_update_status": c.get("LastUpdateStatus"),
                "console": _console(region, f"lambda/home?region={region}#/functions/{c['FunctionName']}")}

    def table():
        t = boto3.client("dynamodb", region_name=region).describe_table(TableName=d["bookings_table"])["Table"]
        return {"name": t["TableName"], "status": t["TableStatus"], "items_approx": t.get("ItemCount"),
                "billing": (t.get("BillingModeSummary") or {}).get("BillingMode", "PROVISIONED")}

    def alarms():
        cw = boto3.client("cloudwatch", region_name=region)
        items = cw.describe_alarms(AlarmNames=d.get("monitoring", {}).get("alarms", []))["MetricAlarms"]
        return {"alarms": [{"name": a["AlarmName"].removeprefix("travel-agent-"), "state": a["StateValue"],
                            "description": a.get("AlarmDescription"), "since": _iso(a.get("StateUpdatedTimestamp"))}
                           for a in items],
                "dashboard": (d.get("monitoring") or {}).get("dashboard_url")}

    def metrics():
        cw = boto3.client("cloudwatch", region_name=region)
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        start = end - timedelta(hours=HOURS)
        gw_dims = [{"Name": "Resource", "Value": d["gateway"]["gateway_arn"]},
                   {"Name": "Operation", "Value": "InvokeGateway"}, {"Name": "Protocol", "Value": "MCP"}]
        svc = [{"Name": "Service", "Value": "FlightTools"}]
        web = [{"Name": "Interface", "Value": "web"}]

        def ms(i, ns, name, dims, stat):
            return {"Id": i, "MetricStat": {"Metric": {"Namespace": ns, "MetricName": name, "Dimensions": dims},
                                            "Period": 3600, "Stat": stat}, "ReturnData": True}
        g_search = ("SEARCH('{AWS/Bedrock/Guardrails,GuardrailArn,GuardrailVersion} MetricName=\"%s\" "
                    + g["guardrail_id"] + "', 'Sum', 3600)")
        q = [
            ms("turns", "TravelAgent", "AgentTurns", web, "Sum"),
            ms("turnlat", "TravelAgent", "AgentTurnLatency", web, "p90"),
            ms("model", "AWS/Bedrock", "Invocations", [{"Name": "ModelId", "Value": model_id}], "Sum"),
            ms("modellat", "AWS/Bedrock", "InvocationLatency", [{"Name": "ModelId", "Value": model_id}], "p90"),
            {"Id": "geval", "Expression": f"SUM({g_search % 'Invocations'})", "Period": 3600},
            {"Id": "gint", "Expression": f"SUM({g_search % 'InvocationsIntervened'})", "Period": 3600},
            ms("gwreq", "AWS/Bedrock-AgentCore", "Invocations", gw_dims, "Sum"),
            ms("gwuerr", "AWS/Bedrock-AgentCore", "UserErrors", gw_dims, "Sum"),
            ms("gwserr", "AWS/Bedrock-AgentCore", "SystemErrors", gw_dims, "Sum"),
            ms("gwlat", "AWS/Bedrock-AgentCore", "Latency", gw_dims, "p90"),
            ms("lminv", "AWS/Lambda", "Invocations", [{"Name": "FunctionName", "Value": d["lambda_arn"].split(":")[-1]}], "Sum"),
            ms("lmerr", "AWS/Lambda", "Errors", [{"Name": "FunctionName", "Value": d["lambda_arn"].split(":")[-1]}], "Sum"),
            ms("tools", "TravelAgent", "ToolInvocations", svc, "Sum"),
            ms("toolerr", "TravelAgent", "ToolErrors", svc, "Sum"),
            ms("serp", "TravelAgent", "SerpApiRequests", svc, "Sum"),
            ms("serperr", "TravelAgent", "SerpApiErrors", svc, "Sum"),
            ms("bookings", "TravelAgent", "BookingsCreated", svc, "Sum"),
        ]
        res = cw.get_metric_data(MetricDataQueries=q, StartTime=start, EndTime=end, ScanBy="TimestampAscending")
        hours = [start + timedelta(hours=i) for i in range(HOURS)]
        out = {}
        for r in res["MetricDataResults"]:
            by = {t.replace(minute=0, second=0, microsecond=0): v for t, v in zip(r["Timestamps"], r["Values"])}
            series = [by.get(h) for h in hours]
            vals = [v for v in series if v is not None]
            out[r["Id"]] = {"series": series, "total": round(sum(vals), 1) if vals else 0,
                            "max": round(max(vals), 1) if vals else None,
                            "latest": next((v for v in reversed(series) if v is not None), None)}
        # Searches left: latest datapoint over the last 7 days (published at most every 15 min).
        left = cw.get_metric_statistics(Namespace="TravelAgent", MetricName="SerpApiSearchesLeft", Dimensions=svc,
                                        StartTime=end - timedelta(days=7), EndTime=end, Period=3600,
                                        Statistics=["Minimum"])["Datapoints"]
        latest_left = min(left, key=lambda p: -p["Timestamp"].timestamp())["Minimum"] if left else None
        return {"hours": [h.isoformat() for h in hours], "m": out, "serpapi_searches_left": latest_left}

    data = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "region": region,
        "model": {"id": model_id, "console": _console(region, f"bedrock/home?region={region}#/")},
        "gateway": _section(gateway), "guardrail": _section(guardrail), "lambda": _section(lambda_fn),
        "table": _section(table), "alarms": _section(alarms), "metrics": _section(metrics),
    }
    with _lock:
        _cache.update(at=time.time(), data=data)
    return data
