#!/usr/bin/env python3
"""
Azure Cost Management MCP Server (Claude Code compatible)

Tools:
- azure_cost_last_full_month_all_subscriptions
- azure_cost_last_full_month_top_resources

Auth:
- Uses your existing `az login` token (no secrets)

RBAC:
- Cost Management Reader on each subscription you want included
"""

import json
import subprocess
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

from mcp.server.fastmcp import FastMCP

AZURE_API_VERSION = "2023-11-01"

mcp = FastMCP("azure-costing")


def run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def az_access_token(resource: str = "https://management.azure.com/") -> str:
    code, out, err = run(
        ["az", "account", "get-access-token", "--resource", resource, "-o", "json"]
    )
    if code != 0:
        raise RuntimeError("Failed to get Azure access token. Run `az login`.\n" + err)
    j = json.loads(out)
    return j["accessToken"]


def az_subscriptions() -> List[Dict[str, Any]]:
    code, out, err = run(["az", "account", "list", "-o", "json"])
    if code != 0:
        raise RuntimeError("Failed to list subscriptions.\n" + err)
    subs = json.loads(out)
    subs = [s for s in subs if s.get("state") == "Enabled"]
    subs.sort(key=lambda s: (s.get("name", ""), s.get("id", "")))
    return subs


def last_full_month_range() -> Tuple[str, str]:
    today = date.today()
    first_this_month = date(today.year, today.month, 1)
    last_month_end = first_this_month - timedelta(days=1)
    last_month_start = date(last_month_end.year, last_month_end.month, 1)
    return last_month_start.isoformat(), last_month_end.isoformat()


def cost_query_payload(
    start_date: str,
    end_date: str,
    group_by: List[str] | None = None,
    top: int | None = None,
) -> Dict[str, Any]:
    group_by = group_by or []
    groupings = [{"type": "Dimension", "name": g} for g in group_by]

    dataset: Dict[str, Any] = {
        "granularity": "None",
        "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
    }

    if groupings:
        dataset["grouping"] = groupings

    if top is not None:
        dataset["sorting"] = [{"direction": "descending", "name": "PreTaxCost"}]
        dataset["top"] = top

    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": start_date, "to": end_date},
        "dataset": dataset,
    }


def http_post_json(url: str, payload: Dict[str, Any], token: str) -> Dict[str, Any]:
    body = json.dumps(payload)
    cmd = [
        "curl",
        "-sS",
        "-X",
        "POST",
        url,
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Content-Type: application/json",
        "--data-binary",
        body,
    ]
    code, out, err = run(cmd)
    if code != 0:
        raise RuntimeError("curl failed: " + err)

    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError("Non-JSON response from Azure API:\n" + out[:800])


def query_cost(scope: str, payload: Dict[str, Any], token: str) -> Dict[str, Any]:
    url = (
        f"https://management.azure.com{scope}"
        f"/providers/Microsoft.CostManagement/query?api-version={AZURE_API_VERSION}"
    )
    return http_post_json(url, payload, token)


def parse_rows(result_json: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Returns:
      rows_as_dicts, column_names
    """
    props = result_json.get("properties", {})
    cols = props.get("columns", [])
    rows = props.get("rows", [])

    col_names = [c.get("name") for c in cols]
    out_rows = []
    for r in rows:
        d = {}
        for i, name in enumerate(col_names):
            if i < len(r):
                d[name] = r[i]
        out_rows.append(d)
    return out_rows, col_names


@mcp.tool()
def azure_cost_last_full_month_all_subscriptions() -> Dict[str, Any]:
    """
    Returns actual cost for the last full calendar month for every Enabled subscription
    the current `az login` identity can access.
    """
    start_date, end_date = last_full_month_range()

    token = az_access_token()
    subs = az_subscriptions()

    results = []
    errors = []

    for s in subs:
        sid = s.get("id")
        sname = s.get("name")
        try:
            scope = f"/subscriptions/{sid}"
            payload = cost_query_payload(start_date, end_date)
            r = query_cost(scope, payload, token)
            rows, _ = parse_rows(r)

            # With no grouping, expect 1 row with PreTaxCost + Currency
            if rows:
                cost = float(rows[0].get("PreTaxCost", 0) or 0)
                cur = rows[0].get("Currency", "")
            else:
                cost = 0.0
                cur = ""

            results.append(
                {
                    "subscriptionName": sname,
                    "subscriptionId": sid,
                    "cost": round(cost, 2),
                    "currency": cur,
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

    return {
        "period": {"from": start_date, "to": end_date},
        "subscriptions": results,
        "errors": errors,
        "notes": [
            "403 errors mean you do not have Cost Management Reader on that subscription.",
            "This is actual billed cost (PreTaxCost) from Cost Management Query API.",
        ],
    }


@mcp.tool()
def azure_cost_last_full_month_top_resources(subscription_id: str, top: int = 25) -> Dict[str, Any]:
    """
    Returns the top N most expensive resources in a given subscription for the last full month.

    Args:
      subscription_id: Azure subscription GUID
      top: how many resources to return (default 25)
    """
    start_date, end_date = last_full_month_range()
    token = az_access_token()

    scope = f"/subscriptions/{subscription_id}"
    payload = cost_query_payload(
        start_date,
        end_date,
        group_by=["ResourceId"],
        top=top,
    )

    r = query_cost(scope, payload, token)
    rows, cols = parse_rows(r)

    # Normalize output
    items = []
    for row in rows:
        items.append(
            {
                "resourceId": row.get("ResourceId"),
                "cost": round(float(row.get("PreTaxCost", 0) or 0), 2),
                "currency": row.get("Currency", ""),
            }
        )

    return {
        "period": {"from": start_date, "to": end_date},
        "subscriptionId": subscription_id,
        "top": top,
        "resources": items,
        "columns": cols,
        "notes": [
            "ResourceId grouping is the best way to find the biggest cost drivers.",
            "Some costs may show as empty ResourceId depending on how charges are recorded.",
        ],
    }


if __name__ == "__main__":
    mcp.run()
