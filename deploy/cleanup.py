#!/usr/bin/env python3
"""
Delete every AWS resource created by deploy.py (reads config/deployment.json).

Usage:  python travel-agent/deploy/cleanup.py [--yes]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy as D  # noqa: E402


def attempt(label: str, fn) -> None:
    try:
        fn()
        print(f"  ✓ {label}")
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
        print(f"  - {label}: {code}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args()
    if not D.DEPLOYMENT_FILE.is_file():
        sys.exit("No config/deployment.json - nothing to clean up.")
    d = json.loads(D.DEPLOYMENT_FILE.read_text())
    region = d["region"]
    print(f"This deletes the Travel Agent gateway, Lambda, Cognito pool, guardrail, bookings table and chart "
          f"bucket in {d['account_id']} / {region}.")
    if not args.yes and input("Type 'delete' to continue: ").strip() != "delete":
        sys.exit("Aborted.")

    print("CloudWatch monitoring")
    cw = boto3.client("cloudwatch", region_name=region)
    logs = boto3.client("logs", region_name=region)
    mon = d.get("monitoring", {})
    if mon:
        attempt(f"dashboard {mon['dashboard']}", lambda: cw.delete_dashboards(DashboardNames=[mon["dashboard"]]))
        attempt(f"{len(mon['alarms'])} alarms", lambda: cw.delete_alarms(AlarmNames=mon["alarms"]))
    for delivery in logs.describe_deliveries().get("deliveries", []):
        if delivery.get("deliverySourceName") == f"{D.PREFIX}-gateway-logs":
            attempt("gateway log delivery", lambda dl=delivery: logs.delete_delivery(id=dl["id"]))
    attempt("gateway log destination", lambda: logs.delete_delivery_destination(name=f"{D.PREFIX}-gateway-logs-cw"))
    attempt("gateway log source", lambda: logs.delete_delivery_source(name=f"{D.PREFIX}-gateway-logs"))
    for group in mon.get("log_groups", []):
        attempt(f"log group {group}", lambda g=group: logs.delete_log_group(logGroupName=g))

    gw = boto3.client("bedrock-agentcore-control", region_name=region)
    gid = d["gateway"]["gateway_id"]
    print("AgentCore Gateway")
    try:
        for t in gw.list_gateway_targets(gatewayIdentifier=gid).get("items", []):
            attempt(f"target {t['name']}", lambda t=t: gw.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"]))
        for _ in range(24):
            if not gw.list_gateway_targets(gatewayIdentifier=gid).get("items"):
                break
            time.sleep(5)
    except gw.exceptions.ResourceNotFoundException:
        pass
    attempt(f"gateway {gid}", lambda: gw.delete_gateway(gatewayIdentifier=gid))

    print("Lambda")
    attempt(D.LAMBDA, lambda: boto3.client("lambda", region_name=region).delete_function(FunctionName=D.LAMBDA))

    print("IAM roles")
    iam = boto3.client("iam")
    for role in (D.LAMBDA_ROLE, D.GATEWAY_ROLE):
        def drop(role=role):
            for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
            for name in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                iam.delete_role_policy(RoleName=role, PolicyName=name)
            iam.delete_role(RoleName=role)
        attempt(role, drop)

    print("Cognito")
    cog = boto3.client("cognito-idp", region_name=region)
    pool = d["cognito"]["user_pool_id"]
    domain = d["cognito"]["token_endpoint"].split("//", 1)[1].split(".auth.", 1)[0]
    attempt(f"domain {domain}", lambda: cog.delete_user_pool_domain(Domain=domain, UserPoolId=pool))
    attempt(f"user pool {pool}", lambda: cog.delete_user_pool(UserPoolId=pool))

    print("Bedrock Guardrail")
    attempt(d["guardrail"]["guardrail_id"], lambda: boto3.client("bedrock", region_name=region)
            .delete_guardrail(guardrailIdentifier=d["guardrail"]["guardrail_id"]))

    print("Secrets Manager")
    attempt(D.SERPAPI_SECRET, lambda: boto3.client("secretsmanager", region_name=region)
            .delete_secret(SecretId=D.SERPAPI_SECRET, ForceDeleteWithoutRecovery=True))

    print("DynamoDB")
    attempt(d["bookings_table"], lambda: boto3.client("dynamodb", region_name=region)
            .delete_table(TableName=d["bookings_table"]))

    print("S3")
    bucket = boto3.resource("s3", region_name=region).Bucket(d["charts_bucket"])
    attempt(f"objects in {bucket.name}", lambda: bucket.objects.all().delete())
    attempt(bucket.name, bucket.delete)

    D.DEPLOYMENT_FILE.unlink()
    print("Removed config/deployment.json - the agent falls back to local mode.")


if __name__ == "__main__":
    main()
