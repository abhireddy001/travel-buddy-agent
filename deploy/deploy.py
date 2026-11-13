#!/usr/bin/env python3
"""
Deploy the Travel Agent's AWS backend (idempotent - safe to re-run).

Creates / updates, all prefixed "travel-agent":
  1. DynamoDB table          bookings + atomic seat counters
  2. S3 bucket               trend-chart reports (private, 7-day expiry; client downloads them)
  3. Lambda function         the 8 flight tools (shared travel_core code)
  4. Cognito user pool       M2M client-credentials auth for the Gateway (CUSTOM_JWT)
  5. AgentCore Gateway       MCP endpoint + Lambda target "FlightTools"
  6. Bedrock Guardrail       PII, harmful content, prompt attacks, travel-safety topics
  7. CloudWatch monitoring   dashboard, alarms, log retention, Gateway log delivery
Writes travel-agent/config/deployment.json (identifiers only - no secrets).

Usage:  python travel-agent/deploy/deploy.py [--region us-east-1]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = "travel-agent"
TABLE = f"{PREFIX}-bookings"
LAMBDA = f"{PREFIX}-flight-tools"
LAMBDA_ROLE = f"{PREFIX}-tools-lambda-role"
GATEWAY = f"{PREFIX}-gateway"
GATEWAY_ROLE = f"{PREFIX}-gateway-role"
TARGET = "FlightTools"
POOL = f"{PREFIX}-pool"
M2M_CLIENT = f"{PREFIX}-m2m-client"
RESOURCE_SERVER = f"{PREFIX}-gateway"
SCOPE = "invoke"
GUARDRAIL = f"{PREFIX}-guardrail"
SERPAPI_SECRET = f"{PREFIX}/serpapi-api-key"
DASHBOARD = "TravelAgent"
METRIC_NS = "TravelAgent"
ALARM_PREFIX = f"{PREFIX}-"
GATEWAY_LOG_GROUP = f"/aws/vendedlogs/bedrock-agentcore/gateway/{GATEWAY}"
LOG_RETENTION_DAYS = 30
DEPLOYMENT_FILE = ROOT / "config" / "deployment.json"


def step(msg: str) -> None:
    print(f"\n▶ {msg}")


def wait_until(fn, ok, fail=(), timeout=180, interval=5, what="resource"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = fn()
        if status in ok:
            return status
        if status in fail:
            raise RuntimeError(f"{what} entered status {status}")
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {what}")


# =============================================================================
# 1. DynamoDB
# =============================================================================

def deploy_table(region: str) -> str:
    step(f"DynamoDB table {TABLE}")
    ddb = boto3.client("dynamodb", region_name=region)
    try:
        ddb.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
            SSESpecification={"Enabled": True},
        )
        print("  created")
    except ddb.exceptions.ResourceInUseException:
        print("  exists")
    ddb.get_waiter("table_exists").wait(TableName=TABLE)
    return ddb.describe_table(TableName=TABLE)["Table"]["TableArn"]


# =============================================================================
# 2. S3
# =============================================================================

def deploy_bucket(region: str, account: str) -> str:
    # Named under the workshop's "ecommerce-workshop-*" prefix: the workshop role may only delete buckets there.
    bucket = f"ecommerce-workshop-travel-charts-{account}-{region}"
    step(f"S3 bucket {bucket}")
    s3 = boto3.client("s3", region_name=region)
    try:
        kwargs = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        s3.create_bucket(Bucket=bucket, **kwargs)
        print("  created")
    except ClientError as e:
        if e.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
        print("  exists")
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [{
        "ID": "expire-charts", "Status": "Enabled", "Filter": {"Prefix": "charts/"}, "Expiration": {"Days": 7}}]})
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
        "Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}))
    return bucket


# =============================================================================
# IAM helper
# =============================================================================

def ensure_role(iam, name: str, service: str, inline: dict, managed: list[str] = ()) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}
    created = False
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                              Tags=[{"Key": "project", "Value": PREFIX}])["Role"]["Arn"]
        created = True
        print(f"  role {name} created")
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  role {name} exists")
    for policy_arn in managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=policy_arn)
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(inline))
    if created:
        time.sleep(12)  # IAM propagation
    return arn


# =============================================================================
# 3. Lambda
# =============================================================================

def build_lambda_zip() -> bytes:
    import tzdata  # bundled so zoneinfo works regardless of the runtime's OS tz database

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ROOT / "gateway" / "lambda_function.py", "lambda_function.py")
        for f in sorted((ROOT / "travel_core").glob("*.py")):
            z.write(f, f"travel_core/{f.name}")
        tz_root = Path(tzdata.__file__).parent
        for f in tz_root.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f"tzdata/{f.relative_to(tz_root)}")
    return buf.getvalue()


def deploy_lambda(region: str, table_arn: str, bucket: str, account: str) -> str:
    step(f"Lambda {LAMBDA}")
    iam = boto3.client("iam")
    role_arn = ensure_role(iam, LAMBDA_ROLE, "lambda.amazonaws.com", {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
             "Resource": table_arn},
            {"Effect": "Allow", "Action": ["s3:PutObject"],
             "Resource": f"arn:aws:s3:::{bucket}/charts/*"},
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
             "Resource": f"arn:aws:secretsmanager:{region}:{account}:secret:{SERPAPI_SECRET}-*"},
        ],
    }, managed=["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])

    lam = boto3.client("lambda", region_name=region)
    code = build_lambda_zip()
    env = {"Variables": {"BOOKINGS_TABLE": TABLE, "CHARTS_BUCKET": bucket, "SERPAPI_SECRET_ID": SERPAPI_SECRET}}
    try:
        lam.get_function(FunctionName=LAMBDA)
        lam.update_function_code(FunctionName=LAMBDA, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=LAMBDA)
        lam.update_function_configuration(FunctionName=LAMBDA, Role=role_arn, Environment=env,
                                          Timeout=60, MemorySize=512, Handler="lambda_function.lambda_handler")
        lam.get_waiter("function_updated_v2").wait(FunctionName=LAMBDA)
        print("  updated")
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=LAMBDA, Runtime="python3.12", Architectures=["arm64"], Role=role_arn,
                    Handler="lambda_function.lambda_handler", Code={"ZipFile": code}, Timeout=60,
                    MemorySize=512, Environment=env, Description="Travel Agent flight tools (AgentCore Gateway target)",
                    Tags={"project": PREFIX},
                )
                break
            except lam.exceptions.InvalidParameterValueException:
                if attempt == 5:
                    raise
                time.sleep(8)  # role not assumable yet
        lam.get_waiter("function_active_v2").wait(FunctionName=LAMBDA)
        print("  created")
    return lam.get_function(FunctionName=LAMBDA)["Configuration"]["FunctionArn"]


# =============================================================================
# 4. Cognito
# =============================================================================

def deploy_cognito(region: str, account: str) -> dict:
    step(f"Cognito user pool {POOL}")
    cog = boto3.client("cognito-idp", region_name=region)
    pool_id = next((p["Id"] for page in cog.get_paginator("list_user_pools").paginate(MaxResults=60)
                    for p in page["UserPools"] if p["Name"] == POOL), None)
    if not pool_id:
        pool_id = cog.create_user_pool(PoolName=POOL)["UserPool"]["Id"]
        print("  pool created")
    else:
        print("  pool exists")

    try:
        cog.describe_resource_server(UserPoolId=pool_id, Identifier=RESOURCE_SERVER)
    except cog.exceptions.ResourceNotFoundException:
        cog.create_resource_server(UserPoolId=pool_id, Identifier=RESOURCE_SERVER, Name=RESOURCE_SERVER,
                                   Scopes=[{"ScopeName": SCOPE, "ScopeDescription": "Invoke travel tools"}])

    domain = f"{PREFIX}-{account}"
    if not cog.describe_user_pool_domain(Domain=domain).get("DomainDescription", {}).get("UserPoolId"):
        cog.create_user_pool_domain(Domain=domain, UserPoolId=pool_id)

    client_id = next((c["ClientId"] for page in cog.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool_id)
                      for c in page["UserPoolClients"] if c["ClientName"] == M2M_CLIENT), None)
    if not client_id:
        client_id = cog.create_user_pool_client(
            UserPoolId=pool_id, ClientName=M2M_CLIENT, GenerateSecret=True,
            AllowedOAuthFlows=["client_credentials"], AllowedOAuthFlowsUserPoolClient=True,
            AllowedOAuthScopes=[f"{RESOURCE_SERVER}/{SCOPE}"], SupportedIdentityProviders=["COGNITO"],
            AccessTokenValidity=60, TokenValidityUnits={"AccessToken": "minutes"},
        )["UserPoolClient"]["ClientId"]
        print("  M2M client created")
    return {
        "user_pool_id": pool_id,
        "client_id": client_id,
        "scope": f"{RESOURCE_SERVER}/{SCOPE}",
        "token_endpoint": f"https://{domain}.auth.{region}.amazoncognito.com/oauth2/token",
        "discovery_url": f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration",
    }


# =============================================================================
# 5. AgentCore Gateway
# =============================================================================

def _clean_schema(schema: dict) -> dict:
    """Reduce FastMCP JSON Schema to the subset AgentCore Gateway inline schemas accept."""
    out: dict = {}
    if "anyOf" in schema:  # Optional[X] -> X
        non_null = [s for s in schema["anyOf"] if s.get("type") != "null"]
        schema = {**non_null[0], **{k: v for k, v in schema.items() if k not in ("anyOf",)}}
    for key in ("type", "description"):
        if key in schema:
            out[key] = schema[key]
    if "properties" in schema:
        out["properties"] = {k: _clean_schema(v) for k, v in schema["properties"].items()}
    if "items" in schema:
        out["items"] = _clean_schema(schema["items"])
    if schema.get("required"):
        out["required"] = schema["required"]
    if "default" in schema and schema["default"] is not None and "description" not in out:
        out["description"] = f"Default: {schema['default']}"
    elif "default" in schema and schema["default"] is not None:
        out["description"] = f"{out['description']} (default: {schema['default']})"
    return out


def tool_schemas() -> list[dict]:
    """Derive Gateway tool schemas from the local MCP server - one source of truth."""
    from mcp_servers import flight_mcp_server as srv  # noqa: WPS433

    tools = asyncio.run(srv.mcp.list_tools())
    return [{
        "name": t.name,
        "description": " ".join(t.description.split()),
        "inputSchema": _clean_schema(t.inputSchema),
    } for t in tools]


def deploy_gateway(region: str, lambda_arn: str, cognito: dict) -> dict:
    step(f"AgentCore Gateway {GATEWAY}")
    iam = boto3.client("iam")
    role_arn = ensure_role(iam, GATEWAY_ROLE, "bedrock-agentcore.amazonaws.com", {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn}],
    })
    gw = boto3.client("bedrock-agentcore-control", region_name=region)
    auth = {"customJWTAuthorizer": {"discoveryUrl": cognito["discovery_url"], "allowedClients": [cognito["client_id"]]}}

    existing = next((g for page in gw.get_paginator("list_gateways").paginate()
                     for g in page["items"] if g["name"] == GATEWAY), None)
    if existing:
        gateway_id = existing["gatewayId"]
        print("  gateway exists")
    else:
        gateway_id = gw.create_gateway(
            name=GATEWAY, roleArn=role_arn, protocolType="MCP", authorizerType="CUSTOM_JWT",
            authorizerConfiguration=auth, description="Travel Agent flight tools",
            tags={"project": PREFIX},
        )["gatewayId"]
        print("  gateway created")
    wait_until(lambda: gw.get_gateway(gatewayIdentifier=gateway_id)["status"], {"READY"},
               {"FAILED"}, what="gateway")
    details = gw.get_gateway(gatewayIdentifier=gateway_id)

    schemas = tool_schemas()
    target_cfg = {"mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": schemas}}}}
    creds = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    target = next((t for page in gw.get_paginator("list_gateway_targets").paginate(gatewayIdentifier=gateway_id)
                   for t in page["items"] if t["name"] == TARGET), None)
    if target:
        target_id = target["targetId"]
        gw.update_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id, name=TARGET,
                                 targetConfiguration=target_cfg, credentialProviderConfigurations=creds)
        print(f"  target {TARGET} updated ({len(schemas)} tools)")
    else:
        target_id = gw.create_gateway_target(
            gatewayIdentifier=gateway_id, name=TARGET, description="Flight search, trends and booking",
            targetConfiguration=target_cfg, credentialProviderConfigurations=creds,
        )["targetId"]
        print(f"  target {TARGET} created ({len(schemas)} tools)")
    wait_until(lambda: gw.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)["status"],
               {"READY"}, {"FAILED", "UPDATE_UNSUCCESSFUL"}, what="gateway target")
    return {"gateway_id": gateway_id, "gateway_arn": details["gatewayArn"], "gateway_url": details["gatewayUrl"],
            "target_name": TARGET}


# =============================================================================
# 6. Bedrock Guardrail
# =============================================================================

# Standard guardrails: PII, harmful content, prompt attacks and two travel-safety topics.
# Names, emails and phone numbers stay allowed (bookings and greetings need them).
PII_BLOCK = (
    "CREDIT_DEBIT_CARD_NUMBER", "CREDIT_DEBIT_CARD_CVV", "CREDIT_DEBIT_CARD_EXPIRY",
    "US_BANK_ACCOUNT_NUMBER", "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER", "US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER", "US_PASSPORT_NUMBER",
    "UK_NATIONAL_INSURANCE_NUMBER", "CA_SOCIAL_INSURANCE_NUMBER",
    "PIN", "PASSWORD", "AWS_ACCESS_KEY", "AWS_SECRET_KEY",
)
GUARDRAIL_CONFIG = {
    "description": "Travel Buddy guardrail: PII (financial, government ID incl. Singapore NRIC/FIN, credentials), "
                   "harmful content, prompt attacks, travel-document fraud and aviation-security evasion.",
    "contentPolicyConfig": {"filtersConfig": [
        {"type": "SEXUAL", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "HATE", "inputStrength": "HIGH", "outputStrength": "HIGH"},
        {"type": "VIOLENCE", "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"},
        {"type": "INSULTS", "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"},
        {"type": "MISCONDUCT", "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"},
        {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"},
    ]},
    "topicPolicyConfig": {"topicsConfig": [
        {"name": "TravelDocumentFraud", "type": "DENY",
         "definition": "Obtaining, forging or using fake, altered or stolen passports, visas, boarding passes or "
                       "ID documents, or deceiving immigration or border officials.",
         "examples": ["How can I get a fake passport to fly abroad?", "Can you edit a boarding pass with someone else's name?"]},
        {"name": "EvadingAviationSecurity", "type": "DENY",
         "definition": "Bringing weapons, explosives or other prohibited items through airport security or onto an "
                       "aircraft, or avoiding or defeating security screening.",
         "examples": ["How do I sneak a knife through airport security?", "Which scanners miss items hidden in luggage?"]},
    ]},
    "sensitiveInformationPolicyConfig": {
        "piiEntitiesConfig": [{"type": t, "action": "BLOCK"} for t in PII_BLOCK],
        "regexesConfig": [{
            "name": "SingaporeNRIC", "description": "Singapore NRIC / FIN number",
            "pattern": "\\b[STFGMstfgm][0-9]{7}[A-Za-z]\\b", "action": "BLOCK",
        }],
    },
    "blockedInputMessaging": (
        "Sorry, I can't help with that 🙏 Please don't share sensitive personal details (card, bank, passport, "
        "NRIC or ID numbers, PINs or passwords) in chat, and keep requests travel-friendly. Where shall we go next?"
    ),
    "blockedOutputsMessaging": "Sorry, I can't share that. Can I help you with your trip instead?",
}


def deploy_guardrail(region: str) -> dict:
    step(f"Bedrock Guardrail {GUARDRAIL}")
    br = boto3.client("bedrock", region_name=region)
    existing = next((g for page in br.get_paginator("list_guardrails").paginate()
                     for g in page["guardrails"] if g["name"] == GUARDRAIL), None)
    if existing:
        gid = existing["id"]
        br.update_guardrail(guardrailIdentifier=gid, name=GUARDRAIL, **GUARDRAIL_CONFIG)
        print("  updated DRAFT")
    else:
        gid = br.create_guardrail(name=GUARDRAIL, tags=[{"key": "project", "value": PREFIX}],
                                  **GUARDRAIL_CONFIG)["guardrailId"]
        print("  created")
    wait_until(lambda: br.get_guardrail(guardrailIdentifier=gid)["status"], {"READY"}, {"FAILED"},
               what="guardrail")
    version = br.create_guardrail_version(guardrailIdentifier=gid,
                                          description="Published by deploy.py")["version"]
    wait_until(lambda: br.get_guardrail(guardrailIdentifier=gid, guardrailVersion=version)["status"],
               {"READY"}, {"FAILED"}, what="guardrail version")
    print(f"  published version {version}")
    return {"guardrail_id": gid, "guardrail_version": version}



# =============================================================================
# 7. CloudWatch monitoring
# =============================================================================

def _ensure_log_group(logs, name: str) -> None:
    try:
        logs.create_log_group(logGroupName=name)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=name, retentionInDays=LOG_RETENTION_DAYS)


def _gateway_log_delivery(region: str, account: str, gateway: dict) -> str | None:
    """Deliver AgentCore Gateway application logs to CloudWatch Logs (best effort)."""
    logs = boto3.client("logs", region_name=region)
    _ensure_log_group(logs, GATEWAY_LOG_GROUP)
    src, dst = f"{PREFIX}-gateway-logs", f"{PREFIX}-gateway-logs-cw"
    try:
        logs.put_delivery_source(name=src, resourceArn=gateway["gateway_arn"], logType="APPLICATION_LOGS")
        dest_arn = logs.put_delivery_destination(
            name=dst, deliveryDestinationConfiguration={
                "destinationResourceArn": f"arn:aws:logs:{region}:{account}:log-group:{GATEWAY_LOG_GROUP}"},
        )["deliveryDestination"]["arn"]
        existing = [d for d in logs.describe_deliveries().get("deliveries", [])
                    if d.get("deliverySourceName") == src]
        if not existing:
            logs.create_delivery(deliverySourceName=src, deliveryDestinationArn=dest_arn)
        print(f"  Gateway application logs -> {GATEWAY_LOG_GROUP}")
        return GATEWAY_LOG_GROUP
    except ClientError as e:
        print(f"  Gateway log delivery skipped ({e.response['Error']['Code']})")
        return None


def _dashboard_body(region: str, d: dict) -> dict:
    gw_arn = d["gateway"]["gateway_arn"]
    g_id = d["guardrail"]["guardrail_id"]
    fn = LAMBDA
    tools = ["search_flights", "get_booking_options", "find_cheapest_dates", "get_price_trends",
             "check_availability", "search_airports", "book_flight", "get_booking", "cancel_booking"]
    ns = METRIC_NS

    def w(x, y, wd, ht, props, kind="metric"):
        props = props if kind == "text" else {"region": region, **props}
        return {"type": kind, "x": x, "y": y, "width": wd, "height": ht, "properties": props}

    def num(title, metrics, stat="Sum", period=86400):
        return {"title": title, "view": "singleValue", "metrics": metrics, "stat": stat, "period": period,
                "setPeriodToTimeRange": True}

    def ts(title, metrics, stat="Sum", period=300, **extra):
        return {"title": title, "view": "timeSeries", "metrics": metrics, "stat": stat, "period": period,
                "stacked": False, **extra}

    gw = ["Resource", gw_arn, "Operation", "InvokeGateway", "Protocol", "MCP"]
    return {"widgets": [
        w(0, 0, 24, 2, {"markdown": (
            "# Travel Agent monitoring\n"
            f"Agent turns and tool calls · Bedrock model + guardrail `{g_id}` · AgentCore Gateway · "
            f"Lambda `{fn}` · live Google Flights (SerpApi) · bookings. "
            "Custom metrics are in the **TravelAgent** namespace.")}, "text"),

        # KPIs (whole selected time range)
        w(0, 2, 4, 4, num("Agent turns", [[ns, "AgentTurns", "Interface", "web", {"label": "web"}],
                                          [ns, "AgentTurns", "Interface", "cli", {"label": "cli"}]])),
        w(4, 2, 4, 4, num("Guardrail interventions", [[{"expression": f"SUM(SEARCH('{{AWS/Bedrock/Guardrails,GuardrailArn,GuardrailVersion}} MetricName=\"InvocationsIntervened\" {g_id}', 'Sum', 86400))", "label": "blocked (card details)", "id": "gi"}]])),
        w(8, 2, 4, 4, num("Live vs simulated tool calls", [[ns, "ToolInvocations", "Service", "FlightTools", "DataSource", "live", {"label": "live"}],
                                                            [ns, "ToolInvocations", "Service", "FlightTools", "DataSource", "simulated", {"label": "simulated"}]])),
        w(12, 2, 4, 4, num("SerpApi searches left", [[ns, "SerpApiSearchesLeft", "Service", "FlightTools", {"label": "searches left"}]], stat="Minimum")),
        w(16, 2, 4, 4, num("Bookings", [[ns, "BookingsCreated", "Service", "FlightTools", {"label": "created"}],
                                        [ns, "BookingsCancelled", "Service", "FlightTools", {"label": "cancelled"}]])),
        w(20, 2, 4, 4, num("Errors", [[ns, "AgentTurnErrors", "Interface", "web", {"label": "agent (web)"}],
                                      [ns, "ToolFailures", "Service", "FlightTools", {"label": "tool failures"}],
                                      ["AWS/Lambda", "Errors", "FunctionName", fn, {"label": "Lambda"}]])),

        # Agent
        w(0, 6, 12, 6, ts("Agent turn latency (ms)", [[ns, "AgentTurnLatency", "Interface", "web", {"stat": "p50", "label": "web p50"}],
                                                      [ns, "AgentTurnLatency", "Interface", "web", {"stat": "p90", "label": "web p90"}],
                                                      [ns, "AgentTurnLatency", "Interface", "cli", {"stat": "p50", "label": "cli p50"}]],
                          stat="p50", yAxis={"left": {"min": 0}})),
        w(12, 6, 12, 6, ts("Agent tokens per 5 min", [[ns, "InputTokens", "Interface", "web", {"label": "input (web)"}],
                                                      [ns, "OutputTokens", "Interface", "web", {"label": "output (web)"}],
                                                      [ns, "InputTokens", "Interface", "cli", {"label": "input (cli)"}],
                                                      [ns, "OutputTokens", "Interface", "cli", {"label": "output (cli)"}]])),

        # Bedrock model + guardrail
        w(0, 12, 8, 6, ts("Bedrock model invocations & latency (account-wide for this model)",
                          [["AWS/Bedrock", "Invocations", "ModelId", "global.anthropic.claude-sonnet-5", {"label": "invocations"}],
                           ["AWS/Bedrock", "InvocationLatency", "ModelId", "global.anthropic.claude-sonnet-5", {"stat": "p90", "label": "latency p90 (ms)", "yAxis": "right"}]])),
        w(8, 12, 8, 6, ts("Guardrail evaluations vs interventions",
                          [[{"expression": f"SUM(SEARCH('{{AWS/Bedrock/Guardrails,GuardrailArn,GuardrailVersion}} MetricName=\"Invocations\" {g_id}', 'Sum', 300))", "id": "ge", "label": "evaluations"}],
                           [{"expression": f"SUM(SEARCH('{{AWS/Bedrock/Guardrails,GuardrailArn,GuardrailVersion}} MetricName=\"InvocationsIntervened\" {g_id}', 'Sum', 300))", "id": "gx", "label": "intervened"}]])),
        w(16, 12, 8, 6, ts("Tool calls per agent turn", [[ns, "ToolCallsPerTurn", "Interface", "web", {"stat": "Average", "label": "web avg"}],
                                                         [ns, "ToolCallsPerTurn", "Interface", "web", {"stat": "Maximum", "label": "web max"}]], stat="Average")),

        # Gateway
        w(0, 18, 8, 6, ts("AgentCore Gateway requests & errors", [["AWS/Bedrock-AgentCore", "Invocations", *gw, {"label": "invocations"}],
                                                                   ["AWS/Bedrock-AgentCore", "UserErrors", *gw, {"label": "user errors"}],
                                                                   ["AWS/Bedrock-AgentCore", "SystemErrors", *gw, {"label": "system errors"}],
                                                                   ["AWS/Bedrock-AgentCore", "Throttles", *gw, {"label": "throttles"}]])),
        w(8, 18, 8, 6, ts("Gateway tool-call latency by tool (ms, p90)",
                          [[{"expression": f"SEARCH('{{AWS/Bedrock-AgentCore,Method,Name,Operation,Protocol,Resource}} MetricName=\"Latency\" Method=\"tools/call\" {GATEWAY}', 'p90', 300)", "id": "gl"}]], stat="p90")),
        w(16, 18, 8, 6, ts("Gateway inbound auth (JWT)",
                           [[{"expression": f"SEARCH('{{AWS/Bedrock-AgentCore,ResourceId}} MetricName=\"InboundAuthorizationSuccess\" {GATEWAY}', 'Sum', 300)", "id": "as", "label": "success"}],
                            [{"expression": f"SEARCH('{{AWS/Bedrock-AgentCore,ExceptionType,ResourceId}} MetricName=\"InboundAuthorizationFailure\" {GATEWAY}', 'Sum', 300)", "id": "af", "label": "failure"}]])),

        # Tools (Lambda)
        w(0, 24, 12, 6, ts("Tool invocations by tool", [[ns, "ToolInvocations", "Service", "FlightTools", "Tool", t, {"label": t}] for t in tools])),
        w(12, 24, 6, 6, ts("Tool errors vs failures", [[ns, "ToolErrors", "Service", "FlightTools", {"label": "errors (returned to agent)"}],
                                                       [ns, "ToolFailures", "Service", "FlightTools", {"label": "failures (exceptions)"}]])),
        w(18, 24, 6, 6, ts("Tool latency (ms)", [[ns, "ToolLatency", "Service", "FlightTools", {"stat": "p50", "label": "p50"}],
                                                 [ns, "ToolLatency", "Service", "FlightTools", {"stat": "p90", "label": "p90"}],
                                                 [ns, "ToolLatency", "Service", "FlightTools", {"stat": "Maximum", "label": "max"}]], stat="p50")),

        # Live data + Lambda + DynamoDB
        w(0, 30, 8, 6, ts("SerpApi (Google Flights) requests", [[ns, "SerpApiRequests", "Service", "FlightTools", "RequestType", "search", {"label": "searches"}],
                                                                [ns, "SerpApiRequests", "Service", "FlightTools", "RequestType", "booking_options", {"label": "booking options"}],
                                                                [ns, "SerpApiErrors", "Service", "FlightTools", {"label": "errors"}],
                                                                [ns, "SerpApiLatency", "Service", "FlightTools", {"stat": "p90", "label": "latency p90 (ms)", "yAxis": "right"}]])),
        w(8, 30, 8, 6, ts("Lambda invocations, errors, throttles", [["AWS/Lambda", "Invocations", "FunctionName", fn],
                                                                    ["AWS/Lambda", "Errors", "FunctionName", fn],
                                                                    ["AWS/Lambda", "Throttles", "FunctionName", fn],
                                                                    ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", fn, {"stat": "Maximum", "yAxis": "right"}]])),
        w(16, 30, 8, 6, ts("DynamoDB bookings table", [["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", TABLE],
                                                       ["AWS/DynamoDB", "ConsumedWriteCapacityUnits", "TableName", TABLE],
                                                       ["AWS/DynamoDB", "ThrottledRequests", "TableName", TABLE]])),

        # Logs
        w(0, 36, 24, 6, {"title": "Recent Lambda errors and warnings", "view": "table",
                         "query": f"SOURCE '/aws/lambda/{fn}' | fields @timestamp, @message "
                                  "| filter @message like /(?i)(error|exception|traceback|warn)/ "
                                  "| filter @message not like /CloudWatchMetrics/ | sort @timestamp desc | limit 50"}, "log"),
    ]}


def _alarms(region: str, d: dict) -> list[dict]:
    gw_dims = [{"Name": "Resource", "Value": d["gateway"]["gateway_arn"]},
               {"Name": "Operation", "Value": "InvokeGateway"}, {"Name": "Protocol", "Value": "MCP"}]
    svc = [{"Name": "Service", "Value": "FlightTools"}]
    base = {"ComparisonOperator": "GreaterThanOrEqualToThreshold", "TreatMissingData": "notBreaching",
            "ActionsEnabled": False}
    return [
        {**base, "AlarmName": f"{ALARM_PREFIX}lambda-errors", "Namespace": "AWS/Lambda", "MetricName": "Errors",
         "Dimensions": [{"Name": "FunctionName", "Value": LAMBDA}], "Statistic": "Sum", "Period": 300,
         "EvaluationPeriods": 1, "Threshold": 1, "AlarmDescription": "Flight-tools Lambda raised errors."},
        {**base, "AlarmName": f"{ALARM_PREFIX}tool-failures", "Namespace": METRIC_NS, "MetricName": "ToolFailures",
         "Dimensions": svc, "Statistic": "Sum", "Period": 300, "EvaluationPeriods": 1, "Threshold": 1,
         "AlarmDescription": "A travel tool hit an unhandled exception."},
        {**base, "AlarmName": f"{ALARM_PREFIX}serpapi-errors", "Namespace": METRIC_NS, "MetricName": "SerpApiErrors",
         "Dimensions": svc, "Statistic": "Sum", "Period": 900, "EvaluationPeriods": 1, "Threshold": 3,
         "AlarmDescription": "Live Google Flights (SerpApi) lookups are failing - check key, quota or outage."},
        {**base, "AlarmName": f"{ALARM_PREFIX}serpapi-quota-low", "Namespace": METRIC_NS,
         "MetricName": "SerpApiSearchesLeft", "Dimensions": svc, "Statistic": "Minimum", "Period": 3600,
         "EvaluationPeriods": 1, "Threshold": 25, "ComparisonOperator": "LessThanThreshold",
         "AlarmDescription": "Fewer than 25 SerpApi searches left this month - live prices will stop soon."},
        {**base, "AlarmName": f"{ALARM_PREFIX}gateway-system-errors", "Namespace": "AWS/Bedrock-AgentCore",
         "MetricName": "SystemErrors", "Dimensions": gw_dims, "Statistic": "Sum", "Period": 300,
         "EvaluationPeriods": 1, "Threshold": 1, "AlarmDescription": "AgentCore Gateway returned system errors."},
        {**base, "AlarmName": f"{ALARM_PREFIX}agent-errors", "Namespace": METRIC_NS, "MetricName": "AgentTurnErrors",
         "Dimensions": [{"Name": "Interface", "Value": "web"}], "Statistic": "Sum", "Period": 300,
         "EvaluationPeriods": 1, "Threshold": 1, "AlarmDescription": "Web chat turns are failing."},
        {**base, "AlarmName": f"{ALARM_PREFIX}agent-latency-p90", "Namespace": METRIC_NS,
         "MetricName": "AgentTurnLatency", "Dimensions": [{"Name": "Interface", "Value": "web"}],
         "ExtendedStatistic": "p90", "Period": 900, "EvaluationPeriods": 1, "Threshold": 60000,
         "ComparisonOperator": "GreaterThanThreshold", "AlarmDescription": "p90 web chat turn latency above 60 s."},
    ]


def deploy_monitoring(region: str, account: str, d: dict) -> dict:
    step("CloudWatch monitoring")
    logs = boto3.client("logs", region_name=region)
    _ensure_log_group(logs, f"/aws/lambda/{LAMBDA}")
    print(f"  /aws/lambda/{LAMBDA}: {LOG_RETENTION_DAYS}-day retention")
    gw_logs = _gateway_log_delivery(region, account, d["gateway"])

    cw = boto3.client("cloudwatch", region_name=region)
    body = _dashboard_body(region, d)
    resp = cw.put_dashboard(DashboardName=DASHBOARD, DashboardBody=json.dumps(body))
    for msg in resp.get("DashboardValidationMessages", []):
        print(f"  dashboard warning: {msg.get('Message')}")
    print(f"  dashboard {DASHBOARD} ({len(body['widgets'])} widgets)")
    alarms = _alarms(region, d)
    for alarm in alarms:
        cw.put_metric_alarm(**alarm)
    print(f"  {len(alarms)} alarms (no notification actions)")
    return {
        "dashboard": DASHBOARD,
        "dashboard_url": f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#dashboards/dashboard/{DASHBOARD}",
        "alarms": [a["AlarmName"] for a in alarms],
        "metric_namespace": METRIC_NS,
        "log_groups": [f"/aws/lambda/{LAMBDA}"] + ([gw_logs] if gw_logs else []),
    }

# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=boto3.session.Session().region_name or "us-east-1")
    args = parser.parse_args()
    region = args.region
    account = boto3.client("sts").get_caller_identity()["Account"]
    print(f"Deploying Travel Agent backend to account {account}, region {region}")

    table_arn = deploy_table(region)
    bucket = deploy_bucket(region, account)
    lambda_arn = deploy_lambda(region, table_arn, bucket, account)
    cognito = deploy_cognito(region, account)
    gateway = deploy_gateway(region, lambda_arn, cognito)
    guardrail = deploy_guardrail(region)

    deployment = {
        "region": region,
        "account_id": account,
        "bookings_table": TABLE,
        "charts_bucket": bucket,
        "lambda_arn": lambda_arn,
        "cognito": cognito,
        "gateway": gateway,
        "guardrail": guardrail,
        "serpapi_secret_id": SERPAPI_SECRET,
    }
    deployment["monitoring"] = deploy_monitoring(region, account, deployment)
    DEPLOYMENT_FILE.write_text(json.dumps(deployment, indent=2) + "\n", encoding="utf-8")
    print(f"\n✅ Done. Wrote {DEPLOYMENT_FILE.relative_to(ROOT.parent)}")
    print(f"   Gateway URL : {gateway['gateway_url']}")
    print(f"   Guardrail   : {guardrail['guardrail_id']} v{guardrail['guardrail_version']}")
    print(f"   Dashboard   : {deployment['monitoring']['dashboard_url']}")


if __name__ == "__main__":
    main()
