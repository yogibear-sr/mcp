#!/usr/bin/env python3
import json
import subprocess
import sys
from datetime import date, timedelta

AZURE_API_VERSION = "2023-11-01"


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def az_access_token(resource="https://management.azure.com/"):
    code, out, err = run(["az", "account", "get-access-token", "--resource", resource, "-o", "json"])
    if code != 0:
        raise RuntimeError(f"Failed to get Azure access token. Run: az login\n{err}")
    j = json.loads(out)
    return j["accessToken"]


def az_subscriptions():
    code, out, err = run(["az", "account", "list", "-o", "json"])
    if code != 0:
        raise RuntimeError(f"Failed to list subscriptions.\n{err}")
    subs = json.loads(out)
    subs = [s for s in subs if s.get("state") == "Enabled"]
    subs.sort(key=lambda s: (s.get("name", ""), s.get("id", "")))
    return subs


def last_full_month_range():
    today = date.today()
    first_this_month = date(today.year, today.month, 1)
    last_month_end = first_this_month - timedelta(days=1)
    last_month_start = date(last_month_end.year, last_month_end.month, 1)
    return last_month_start.isoformat(), last_month_end.isoformat()


def cost_query_payload(start_date, end_date):
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": start_date, "to": end_date},
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
        },
    }


def http_post_json(url, payload, token):
    body = json.dumps(payload)
    cmd = [
        "curl", "-sS",
        "-X", "POST",
        url,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "--data-binary", body,
    ]
    code, out, err = run(cmd)
    if code != 0:
        raise RuntimeError(f"curl failed: {err}")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response from Azure API:\n{out[:800]}")


def query_subscription_cost(subscription_id, start_date, end_date, token):
    scope = f"/subscriptions/{subscription_id}"
    url = f"https://management.azure.com{scope}/providers/Microsoft.CostManagement/query?api-version={AZURE_API_VERSION}"
    payload = cost_query_payload(start_date, end_date)
    return http_post_json(url, payload, token)


def parse_total_cost(result_json):
    props = result_json.get("properties", {})
    rows = props.get("rows", [])
    cols = props.get("columns", [])
    if not rows:
        return 0.0, None
    cost_idx = next((i for i, c in enumerate(cols) if c.get("name") == "PreTaxCost"), 0)
    cur_idx = next((i for i, c in enumerate(cols) if c.get("name") == "Currency"), None)
    row = rows[0]
    cost = float(row[cost_idx]) if row[cost_idx] is not None else 0.0
    cur = row[cur_idx] if cur_idx is not None and cur_idx < len(row) else None
    return cost, cur


TOOLS = [
    {
        "name": "azure_cost_last_full_month_all_subscriptions",
        "description": "Return actual cost for the last full calendar month for every Enabled subscription you can access. Requires Cost Management Reader on each subscription.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    }
]


def mcp_send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def handle_tools_list():
    mcp_send({"tools": TOOLS})


def handle_tools_call(msg):
    name = msg.get("name")
    if name != "azure_cost_last_full_month_all_subscriptions":
        mcp_send({"error": f"Unknown tool: {name}"})
        return

    start_date, end_date = last_full_month_range()
    token = az_access_token()
    subs = az_subscriptions()

    results = []
    errors = []

    for s in subs:
        sid = s.get("id")
        sname = s.get("name")
        try:
            r = query_subscription_cost(sid, start_date, end_date, token)
            cost, cur = parse_total_cost(r)
            results.append(
                {
                    "subscriptionName": sname,
                    "subscriptionId": sid,
                    "cost": round(cost, 2),
                    "currency": cur,
                    "periodStart": start_date,
                    "periodEnd": end_date,
                }
            )
        except Exception as ex:
            errors.append(
                {
                    "subscriptionName": sname,
                    "subscriptionId": sid,
                    "error": str(ex)[:500],
                }
            )

    results.sort(key=lambda x: x["cost"], reverse=True)

    mcp_send(
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "period": {"from": start_date, "to": end_date},
                            "subscriptions": results,
                            "errors": errors,
                            "notes": [
                                "403 errors mean you do not have Cost Management Reader on that subscription.",
                                "This is actual billed cost (PreTaxCost) from Cost Management Query API.",
                            ],
                        },
                        indent=2,
                    ),
                }
            ]
        }
    )


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            mcp_send({"error": "Invalid JSON"})
            continue

        method = msg.get("method")
        if method == "tools/list":
            handle_tools_list()
        elif method == "tools/call":
            handle_tools_call(msg)
        else:
            mcp_send({"error": f"Unknown method: {method}"})


if __name__ == "__main__":
    main()
