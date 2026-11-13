#!/usr/bin/env python3
"""
Store your SerpApi key so the Travel Agent shows LIVE Google Flights prices.

    python travel-agent/deploy/set_serpapi_key.py

The key is typed at a hidden prompt (or read from $SERPAPI_API_KEY), checked against SerpApi,
and stored in AWS Secrets Manager as "travel-agent/serpapi-api-key". It is never written to disk
or printed. The Gateway Lambda picks it up within ~5 minutes (immediately on a cold start).
Get a key at https://serpapi.com/manage-api-key
"""

import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy as D  # noqa: E402


def main() -> None:
    key = os.environ.get("SERPAPI_API_KEY") or getpass.getpass("SerpApi API key (hidden): ")
    key = key.strip()
    if not key:
        sys.exit("No key entered.")
    try:
        with urllib.request.urlopen("https://serpapi.com/account.json?" + urllib.parse.urlencode({"api_key": key}),
                                    timeout=20) as r:
            acct = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"SerpApi rejected the key (HTTP {e.code}). Nothing was stored.")
    if acct.get("error"):
        sys.exit(f"SerpApi rejected the key: {acct['error']}. Nothing was stored.")
    print(f"Key OK - plan: {acct.get('plan_name', '?')}, searches left this month: "
          f"{acct.get('total_searches_left', acct.get('plan_searches_left', '?'))}")

    region = json.loads(D.DEPLOYMENT_FILE.read_text())["region"] if D.DEPLOYMENT_FILE.is_file() else "us-east-1"
    sm = boto3.client("secretsmanager", region_name=region)
    try:
        sm.put_secret_value(SecretId=D.SERPAPI_SECRET, SecretString=key)
        print(f"Updated secret {D.SERPAPI_SECRET} in {region}.")
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(Name=D.SERPAPI_SECRET, SecretString=key,
                         Description="SerpApi key for the Travel Agent (live Google Flights prices)")
        print(f"Created secret {D.SERPAPI_SECRET} in {region}.")
    print("Done. Start a New chat in the Travel Agent to get live prices.")


if __name__ == "__main__":
    main()
