"""
Travel Agent - books flights worldwide, checks availability, finds the
cheapest options and shows fare trends.

Strands Agent on Amazon Bedrock (Claude), protected by a Bedrock Guardrail,
calling flight tools through an Amazon Bedrock AgentCore Gateway (MCP over
HTTPS, Cognito JWT auth) backed by AWS Lambda + DynamoDB + S3.

Modes
-----
gateway (default once deploy/deploy.py has written config/deployment.json)
local   (--local) tools run in a local stdio MCP server; useful offline / for dev

Usage:
    python agents/travel_agent.py                       # interactive chat
    python agents/travel_agent.py -q "Cheapest JFK to LHR next month?"
    python agents/travel_agent.py --local --no-guardrail
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import date
from pathlib import Path

import boto3
import httpx
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
import re

from strands import Agent, tool
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

SECTION_DIR = Path(__file__).resolve().parents[1]
CONFIG = json.loads((SECTION_DIR / "config" / "agent_config.json").read_text(encoding="utf-8"))
DEPLOYMENT_FILE = SECTION_DIR / "config" / "deployment.json"

logging.basicConfig(format="%(levelname)s | %(name)s | %(message)s", level=logging.WARNING)

GUARDRAIL_INPUT_REDACTION = "[Message removed by the travel agent's safety guardrail.]"


def load_deployment() -> dict | None:
    if DEPLOYMENT_FILE.is_file():
        return json.loads(DEPLOYMENT_FILE.read_text(encoding="utf-8"))
    return None


def build_system_prompt(today: date | None = None) -> str:
    template = (SECTION_DIR / CONFIG["system_prompt_file"]).read_text(encoding="utf-8")
    today = today or date.today()
    return template.replace("{today}", f"{today.isoformat()} ({today:%A})")


class CognitoClientCredentialsAuth(httpx.Auth):
    """
    httpx auth that attaches a Cognito M2M access token and refreshes it before
    expiry. The client secret is read from Cognito with the caller's AWS
    credentials at runtime - it is never written to disk.
    """

    def __init__(self, cognito: dict, region: str):
        self.cognito = cognito
        self.region = region
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _client_secret(self) -> str:
        cog = boto3.client("cognito-idp", region_name=self.region)
        return cog.describe_user_pool_client(
            UserPoolId=self.cognito["user_pool_id"], ClientId=self.cognito["client_id"],
        )["UserPoolClient"]["ClientSecret"]

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 120:
                return self._token
            resp = httpx.post(
                self.cognito["token_endpoint"],
                data={"grant_type": "client_credentials", "scope": self.cognito["scope"]},
                auth=(self.cognito["client_id"], self._client_secret()),
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            self._token = body["access_token"]
            self._expires_at = time.time() + int(body.get("expires_in", 3600))
            return self._token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.token()}"
        yield request


class ChartDownloadHook(HookProvider):
    """
    In gateway mode the Lambda stores trend reports in S3 and returns an s3:// URI.
    Before the model sees the tool result, download the report into output/ and
    replace the URI with the local path - so no S3 links or credentials ever pass
    through the model, and the user gets a file they can open directly.
    """

    def __init__(self, region: str, output_dir: Path = SECTION_DIR / "output"):
        self.s3 = boto3.client("s3", region_name=region)
        self.output_dir = output_dir

    def register_hooks(self, registry: HookRegistry, **_) -> None:
        registry.add_callback(AfterToolCallEvent, self.on_tool_result)

    def on_tool_result(self, event: AfterToolCallEvent) -> None:
        if not event.tool_use["name"].endswith("get_price_trends") or event.result.get("status") != "success":
            return
        content = list(event.result.get("content", []))
        for i, block in enumerate(content):
            payload = _parse_json_text(block.get("text"))
            uri = payload.get("chart_file", "") if payload else ""
            if not uri.startswith("s3://"):
                continue
            bucket, key = uri[5:].split("/", 1)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            local = self.output_dir / Path(key).name
            try:
                self.s3.download_file(bucket, key, str(local))
                payload["chart_file"] = str(local)
            except Exception as e:  # keep the turn going even if the download fails
                logging.getLogger(__name__).warning("Chart download failed: %s", type(e).__name__)
                payload["chart_file"] = "unavailable (chart download failed)"
            content[i] = {"text": json.dumps(payload, ensure_ascii=False)}
        event.result = {**event.result, "content": content}


def _parse_json_text(text: str | None) -> dict | None:
    """Tool text may be a JSON object, or a JSON string containing one."""
    if not text:
        return None
    try:
        value = json.loads(text)
        if isinstance(value, str):
            value = json.loads(value)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


class TurnMetrics:
    """
    Publishes one set of CloudWatch metrics per agent turn (namespace "TravelAgent",
    dimension Interface=web|cli) from a background thread, so a CloudWatch hiccup
    never slows or breaks the conversation. Disable with TRAVEL_AGENT_METRICS=off.
    """

    NAMESPACE = "TravelAgent"

    def __init__(self, region: str, interface: str):
        self.enabled = os.environ.get("TRAVEL_AGENT_METRICS", "on").lower() not in {"0", "off", "false"}
        self.interface = interface
        self.cw = boto3.client("cloudwatch", region_name=region) if self.enabled else None
        self._pending: list[threading.Thread] = []

    def publish(self, latency_ms: float, *, error: bool, guardrail: bool, tool_calls: int,
                input_tokens: int, output_tokens: int) -> None:
        if not self.enabled:
            return
        dims = [{"Name": "Interface", "Value": self.interface}]
        data = [
            ("AgentTurns", 1, "Count"), ("AgentTurnErrors", int(error), "Count"),
            ("AgentTurnLatency", latency_ms, "Milliseconds"), ("GuardrailInterventions", int(guardrail), "Count"),
            ("ToolCallsPerTurn", tool_calls, "Count"), ("InputTokens", input_tokens, "Count"),
            ("OutputTokens", output_tokens, "Count"),
        ]
        payload = [{"MetricName": n, "Dimensions": dims, "Value": float(v), "Unit": u} for n, v, u in data]

        def send():
            try:
                self.cw.put_metric_data(Namespace=self.NAMESPACE, MetricData=payload)
            except Exception as e:  # monitoring must never break the chat
                logging.getLogger(__name__).debug("metric publish failed: %s", type(e).__name__)

        t = threading.Thread(target=send, daemon=True)
        t.start()
        self._pending = [x for x in self._pending if x.is_alive()] + [t]

    def flush(self, timeout: float = 3.0) -> None:
        """Wait briefly for in-flight publishes (so short CLI runs don't drop their metrics)."""
        deadline = time.monotonic() + timeout
        for t in self._pending:
            t.join(max(0.0, deadline - time.monotonic()))


def _usage(result) -> tuple[int, int, int]:
    """(input tokens, output tokens, tool calls) from a Strands AgentResult, defensively."""
    m = getattr(result, "metrics", None)
    usage = getattr(m, "accumulated_usage", None) or {}
    tool_metrics = getattr(m, "tool_metrics", None) or {}
    calls = sum(getattr(t, "call_count", 0) for t in tool_metrics.values())
    return int(usage.get("inputTokens", 0)), int(usage.get("outputTokens", 0)), int(calls)


GREETING = ("Hi there, and welcome aboard! 👋 I'm Travel Buddy, your AI trip planner. Pack your bags and follow with Abhi! "
            "Before we start, how may I address you?")
_NAME_OK = re.compile(r"^[^\W\d_][\w .'\-]{0,38}$", re.UNICODE)


_PREF_OK = re.compile(r"^[^\W_][\w &'\-/.,]{0,38}$", re.UNICODE)
PROFILE_LISTS = ("interests", "travel_style", "destinations", "dislikes")


def clean_pref(raw) -> str | None:
    """A short preference phrase like "street food", "beaches", "family", "Bali" (no markup, no sentences)."""
    v = " ".join(str(raw or "").strip().strip(".!").split())
    return v.lower() if v and _PREF_OK.match(v) and len(v.split()) <= 4 else None


def merge_profile(profile: dict, update: dict) -> dict:
    """Merge new preferences into the profile: lists are de-duplicated (newest last, max 10); budget replaces."""
    for key in PROFILE_LISTS:
        vals = update.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        cur = profile.setdefault(key, [])
        for v in vals:
            c = clean_pref(v)
            if not c:
                continue
            if key == "destinations":
                c = c.title()
            if c in cur:
                cur.remove(c)
            cur.append(c)
        del cur[:-10]
        # a new like removes an old dislike of the same thing (and vice versa)
    for like in profile.get("interests", []):
        if like in profile.get("dislikes", []):
            profile["dislikes"].remove(like)
    budget = clean_pref(update.get("budget"))
    if budget:
        profile["budget"] = budget
    return profile


def clean_name(raw: str) -> str | None:
    """A short, plausible personal name (letters, spaces, . ' -), title-cased if typed in lower case."""
    name = " ".join((raw or "").strip().strip(".!,").split())
    if not name or not _NAME_OK.match(name) or any(ch.isdigit() for ch in name):
        return None
    return name.title() if name.islower() else name


class TravelAgent:
    """Flight-booking travel agent."""

    def __init__(self, region: str | None = None, model_id: str | None = None,
                 stream_to_console: bool = False, use_gateway: bool | None = None,
                 use_guardrail: bool = True, interface: str = "cli"):
        self.deployment = load_deployment()
        self.region = (region or os.environ.get("AWS_REGION")
                       or (self.deployment or {}).get("region") or "us-east-1")
        self.model_id = model_id or os.environ.get("TRAVEL_AGENT_MODEL_ID", CONFIG["model"]["model_id"])

        if use_gateway is None:
            use_gateway = self.deployment is not None
        if use_gateway and not self.deployment:
            raise RuntimeError("Gateway mode needs config/deployment.json - run deploy/deploy.py first.")
        self.mode = "gateway" if use_gateway else "local"
        self.guardrail = (self.deployment or {}).get("guardrail") if use_guardrail else None
        if use_guardrail and not self.guardrail:
            logging.getLogger(__name__).warning("No guardrail deployed; running without Bedrock Guardrails.")

        self.mcp_client = MCPClient(self._transport())
        self.mcp_client.__enter__()
        self.tools = self.mcp_client.list_tools_sync()

        model_kwargs = {}
        if self.guardrail:
            model_kwargs = {
                "guardrail_id": self.guardrail["guardrail_id"],
                "guardrail_version": self.guardrail["guardrail_version"],
                "guardrail_trace": "enabled",
                # Tag only the latest user text turn as guarded input, so the
                # prompt-attack filter applies to what the user typed, not tool output.
                "guardrail_latest_message": True,
                # Remove blocked user input (e.g. a card number) from conversation history.
                "guardrail_redact_input": True,
                "guardrail_redact_input_message": GUARDRAIL_INPUT_REDACTION,
            }
        model = BedrockModel(
            model_id=self.model_id,
            region_name=self.region,
            max_tokens=CONFIG["model"]["max_tokens"],
            **model_kwargs,
        )
        kwargs = {} if stream_to_console else {"callback_handler": None}
        self.traveller_name: str | None = None
        self.profile: dict = {"interests": [], "travel_style": [], "destinations": [], "dislikes": [], "budget": None}
        self._base_prompt = build_system_prompt()

        @tool
        def remember_traveller_name(name: str) -> str:
            """
            Save how the traveller wants to be addressed, as soon as they tell you (e.g. "I'm Priya", "call me Sam").
            Call this once with just the name; afterwards always address them by it.
            """
            cleaned = clean_name(name)
            if not cleaned:
                return json.dumps({"error": "That doesn't look like a name. Ask the traveller again, politely."})
            self.traveller_name = cleaned
            self.agent.system_prompt = self._prompt_with_name()
            return json.dumps({"saved": True, "name": cleaned})

        @tool
        def remember_traveller_preferences(interests: list[str] | None = None, budget: str | None = None,
                                           travel_style: list[str] | None = None,
                                           destinations: list[str] | None = None,
                                           dislikes: list[str] | None = None) -> str:
            """
            Save what the traveller likes, as soon as the chat reveals it, so recommendations follow their
            interests for the rest of the chat. Short phrases only. interests e.g. ["street food", "beaches",
            "temples", "shopping", "nightlife", "hiking", "spa"]; budget: "budget" | "mid-range" | "luxury";
            travel_style e.g. ["family", "couple", "solo", "backpacker"]; destinations they are considering e.g.
            ["Bali"]; dislikes e.g. ["crowds", "long flights"]. Only pass what is new or changed.
            """
            merge_profile(self.profile, {"interests": interests, "budget": budget, "travel_style": travel_style,
                                         "destinations": destinations, "dislikes": dislikes})
            self.agent.system_prompt = self._prompt_with_name()
            return json.dumps({"saved": True, "profile": self.profile})

        self._name_tool = remember_traveller_name
        self.agent = Agent(
            name=CONFIG["agent_name"],
            model=model,
            system_prompt=self._base_prompt,
            tools=[*self.tools, remember_traveller_name, remember_traveller_preferences],
            hooks=[ChartDownloadHook(self.region)] if self.mode == "gateway" else [],
            **kwargs,
        )
        self.last_result = None
        self.metrics = TurnMetrics(self.region, interface)

    def _transport(self):
        if self.mode == "gateway":
            url = self.deployment["gateway"]["gateway_url"]
            auth = CognitoClientCredentialsAuth(self.deployment["cognito"], self.deployment["region"])
            auth.token()  # fail fast on auth problems
            return lambda: streamablehttp_client(url=url, auth=auth)
        server_path = SECTION_DIR / CONFIG["mcp_server"]["path"]
        params = StdioServerParameters(command=sys.executable, args=[str(server_path)], env={**os.environ})
        return lambda: stdio_client(params)

    def __call__(self, message: str) -> str:
        started = time.perf_counter()
        try:
            self.last_result = self.agent(message)
        except Exception:
            self.metrics.publish((time.perf_counter() - started) * 1000, error=True, guardrail=False,
                                 tool_calls=0, input_tokens=0, output_tokens=0)
            raise
        in_tok, out_tok, calls_total = _usage(self.last_result)
        # Strands accumulates usage over the agent's lifetime; publish this turn's delta.
        prev = getattr(self, "_usage_seen", (0, 0, 0))
        self._usage_seen = (in_tok, out_tok, calls_total)
        self.metrics.publish(
            (time.perf_counter() - started) * 1000, error=False, guardrail=self.guardrail_intervened,
            tool_calls=max(0, calls_total - prev[2]), input_tokens=max(0, in_tok - prev[0]),
            output_tokens=max(0, out_tok - prev[1]),
        )
        return str(self.last_result)

    @property
    def guardrail_intervened(self) -> bool:
        return getattr(self.last_result, "stop_reason", None) == "guardrail_intervened"

    def _prompt_with_name(self) -> str:
        parts = []
        if self.traveller_name:
            parts.append(f"The traveller's name is **{self.traveller_name}**. Address them by name naturally "
                         "(greetings, key recommendations, sign-offs), not in every sentence. Use this name for "
                         "bookings only if they confirm it is the passenger's full name.")
        prof = self.profile
        known = {k: v for k, v in prof.items() if v}
        if known:
            lines = [f"- {k.replace('_', ' ').title()}: {', '.join(v) if isinstance(v, list) else v}" for k, v in known.items()]
            parts.append("What you've learned about this traveller so far (keep tailoring every recommendation to it, "
                         "and keep it updated with `remember_traveller_preferences`):\n" + "\n".join(lines))
        if not parts:
            return self._base_prompt
        return self._base_prompt + "\n\n## The traveller\n" + "\n\n".join(parts)

    def tool_names(self) -> list[str]:
        return [getattr(t, "tool_name", getattr(t, "name", "")) for t in self.tools]

    def describe(self) -> str:
        g = f"guardrail {self.guardrail['guardrail_id']} v{self.guardrail['guardrail_version']}" \
            if self.guardrail else "no guardrail"
        return f"mode={self.mode} · region={self.region} · {g} · {len(self.tools)} tools"

    def cleanup(self) -> None:
        if getattr(self, "metrics", None):
            self.metrics.flush()
        if getattr(self, "mcp_client", None):
            try:
                self.mcp_client.__exit__(None, None, None)
            except Exception:
                pass
            self.mcp_client = None

    def __enter__(self) -> "TravelAgent":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Global flight-booking travel agent")
    parser.add_argument("-q", "--query", help="Ask a single question and exit")
    parser.add_argument("--region", help="AWS region for Bedrock (default: deployment region / $AWS_REGION)")
    parser.add_argument("--model-id", help="Bedrock model id override")
    parser.add_argument("--local", action="store_true", help="Use the local stdio MCP server instead of the Gateway")
    parser.add_argument("--no-guardrail", action="store_true", help="Disable the Bedrock Guardrail")
    args = parser.parse_args()

    with TravelAgent(region=args.region, model_id=args.model_id, stream_to_console=True,
                     use_gateway=False if args.local else None,
                     use_guardrail=not args.no_guardrail) as agent:
        print(f"✈️  Travel Agent ready ({agent.describe()})")

        def ask(msg: str) -> None:
            agent(msg)
            print()
            if agent.guardrail_intervened:
                print("🛡️  (Bedrock Guardrail intervened on this turn)")

        if args.query:
            ask(args.query)
            return
        print(f"\nTravel Buddy › {GREETING}  (type 'exit' to quit)\n")
        while True:
            try:
                msg = input("you › ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if msg.lower() in {"exit", "quit", "bye"}:
                break
            if not msg:
                continue
            print("agent › ", end="", flush=True)
            ask(msg)
            print()


if __name__ == "__main__":
    main()
