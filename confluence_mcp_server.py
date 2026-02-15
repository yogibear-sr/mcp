#!/usr/bin/env python3
"""
Confluence Cloud MCP Server (Claude Code compatible)

- Lets Claude create/update Confluence pages
- Designed for "overwrite content but keep Confluence page history"
- Auth: Atlassian email + API token (Basic auth)

You will set env vars before adding the MCP server:
  export CONFLUENCE_BASE_URL="https://group.atlassian.net"
  export CONFLUENCE_EMAIL="your.name@group.co.uk"
  export CONFLUENCE_API_TOKEN="ATATT3xFf...."

Add to Claude Code:
  claude mcp add confluence -- python3 ~/mcp/confluence_mcp_server.py

Tools:
- confluence_get_page_by_title(space_key, title)
- confluence_create_page(space_key, title, content_markdown, parent_id=None)
- confluence_overwrite_page(page_id, title, content_markdown)

Fix included:
- URL-encode the title in confluence_get_page_by_title so punctuation like "–" works.
"""

import base64
import json
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("confluence")


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


def _auth_header() -> str:
    base = _require_env("CONFLUENCE_BASE_URL").rstrip("/")
    email = _require_env("CONFLUENCE_EMAIL")
    token = _require_env("CONFLUENCE_API_TOKEN")

    # Atlassian: Basic base64(email:token)
    raw = f"{email}:{token}".encode("utf-8")
    b64 = base64.b64encode(raw).decode("utf-8")
    return base, f"Authorization: Basic {b64}"


def run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def http_json(
    method: str,
    url: str,
    headers: List[str],
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cmd = ["curl", "-sS", "-X", method, url]
    for h in headers:
        cmd += ["-H", h]
    cmd += ["-H", "Accept: application/json"]

    if payload is not None:
        cmd += [
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            json.dumps(payload),
        ]

    code, out, err = run(cmd)
    if code != 0:
        raise RuntimeError(f"curl failed: {err}")

    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response from Confluence:\n{out[:800]}")


def md_to_confluence_storage(md: str) -> str:
    """
    Confluence Cloud supports "atlas_doc_format" and "storage".
    The simplest API update is storage (XHTML). Proper MD->XHTML conversion is non-trivial.
    Practical approach:
      - wrap markdown in <pre> so it renders cleanly
      - OR send minimal HTML

    We'll use <pre> for reliability.
    """
    escaped = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre>{escaped}</pre>"


@mcp.tool()
def confluence_get_page_by_title(space_key: str, title: str) -> Dict[str, Any]:
    """
    Find a page by title in a space. Returns page id if found.
    """
    base, auth = _auth_header()

    # IMPORTANT: URL-encode title so punctuation and unicode (like "–") works
    safe_title = quote(title, safe="")

    url = (
        f"{base}/wiki/rest/api/content"
        f"?spaceKey={space_key}"
        f"&title={safe_title}"
        f"&expand=version"
    )
    j = http_json("GET", url, [auth])
    results = j.get("results", [])
    if not results:
        return {"found": False, "spaceKey": space_key, "title": title}

    p = results[0]
    return {
        "found": True,
        "id": p.get("id"),
        "title": p.get("title"),
        "version": (p.get("version") or {}).get("number"),
        "url": (p.get("_links") or {}).get("webui"),
    }


@mcp.tool()
def confluence_create_page(
    space_key: str,
    title: str,
    content_markdown: str,
    parent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a new Confluence page.
    """
    base, auth = _auth_header()

    storage = md_to_confluence_storage(content_markdown)

    payload: Dict[str, Any] = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {"storage": {"value": storage, "representation": "storage"}},
    }

    if parent_id:
        payload["ancestors"] = [{"id": str(parent_id)}]

    url = f"{base}/wiki/rest/api/content"
    j = http_json("POST", url, [auth], payload)

    return {
        "created": True,
        "id": j.get("id"),
        "title": j.get("title"),
        "spaceKey": space_key,
        "url": (j.get("_links") or {}).get("webui"),
    }


@mcp.tool()
def confluence_overwrite_page(page_id: str, title: str, content_markdown: str) -> Dict[str, Any]:
    """
    Overwrite a page's content while keeping history (Confluence increments version).
    """
    base, auth = _auth_header()

    # Get current version
    get_url = f"{base}/wiki/rest/api/content/{page_id}?expand=version"
    current = http_json("GET", get_url, [auth])
    current_ver = (current.get("version") or {}).get("number")
    if not current_ver:
        raise RuntimeError("Could not determine current Confluence page version.")

    new_ver = int(current_ver) + 1
    storage = md_to_confluence_storage(content_markdown)

    payload = {
        "id": str(page_id),
        "type": "page",
        "title": title,
        "version": {"number": new_ver},
        "body": {"storage": {"value": storage, "representation": "storage"}},
    }

    put_url = f"{base}/wiki/rest/api/content/{page_id}"
    updated = http_json("PUT", put_url, [auth], payload)

    return {
        "updated": True,
        "id": updated.get("id"),
        "title": updated.get("title"),
        "newVersion": (updated.get("version") or {}).get("number"),
        "url": (updated.get("_links") or {}).get("webui"),
    }


if __name__ == "__main__":
    mcp.run()
