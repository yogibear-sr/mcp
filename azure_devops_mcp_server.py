#!/usr/bin/env python3
"""
azure_devops_mcp_server.py

A minimal, practical MCP server for Azure DevOps that authenticates using a PAT.
It exposes a few useful tools:
- azdo_list_projects
- azdo_list_repos
- azdo_get_file
- azdo_update_file_and_create_pr

Auth:
  Uses AZDO_ORG_URL and AZDO_PAT environment variables.

Example:
  export AZDO_ORG_URL="https://dev.azure.com/YOUR_ORG"
  export AZDO_PAT="YOUR_PAT"

  python azure_devops_mcp_server.py
"""

import os
import json
import base64
import urllib.parse
from typing import Any, Dict, Optional, List

import requests

# --- MCP (FastMCP) ---
# The official Python MCP package is commonly "mcp".
# If you don't have it:
#   pip install mcp
try:
    from mcp.server.fastmcp import FastMCP
except Exception as e:
    raise SystemExit(
        "Missing dependency: mcp\n"
        "Install with: pip install mcp\n"
        f"Original error: {e}"
    )

mcp = FastMCP("azure-devops")


def _env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


def _org_url() -> str:
    # Accept either https://dev.azure.com/org or https://org.visualstudio.com
    url = _env("AZDO_ORG_URL").rstrip("/")
    return url


def _pat() -> str:
    return _env("AZDO_PAT")


def _headers() -> Dict[str, str]:
    # Azure DevOps: Basic base64(:PAT)
    token = base64.b64encode(f":{_pat()}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "azure-devops-mcp/1.0",
    }


def _request(method: str, url: str, **kwargs) -> Any:
    h = kwargs.pop("headers", {})
    headers = {**_headers(), **h}
    resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    if resp.status_code >= 400:
        # Provide helpful error
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise RuntimeError(
            f"Azure DevOps API error {resp.status_code}\nURL: {url}\nResponse: {body}"
        )
    if resp.text.strip() == "":
        return None
    try:
        return resp.json()
    except Exception:
        return resp.text


def _api(url_path: str, api_version: str = "7.1-preview.1") -> str:
    # url_path must start with /
    base = _org_url()
    if not url_path.startswith("/"):
        url_path = "/" + url_path
    return f"{base}{url_path}?api-version={urllib.parse.quote(api_version)}"


# -------------------------
# Tools
# -------------------------

@mcp.tool()
def azdo_list_projects() -> Dict[str, Any]:
    """
    List Azure DevOps projects in the org.
    """
    url = _api("/_apis/projects", api_version="7.1-preview.4")
    data = _request("GET", url)
    projects = [
        {"id": p.get("id"), "name": p.get("name"), "state": p.get("state")}
        for p in data.get("value", [])
    ]
    return {"count": len(projects), "projects": projects}


@mcp.tool()
def azdo_list_repos(project: str) -> Dict[str, Any]:
    """
    List Git repositories in a given Azure DevOps project.

    Args:
      project: Azure DevOps project name
    """
    project_enc = urllib.parse.quote(project)
    url = _api(f"/{project_enc}/_apis/git/repositories", api_version="7.1-preview.1")
    data = _request("GET", url)
    repos = []
    for r in data.get("value", []):
        repos.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "webUrl": r.get("webUrl"),
            "remoteUrl": r.get("remoteUrl"),
            "defaultBranch": r.get("defaultBranch"),
        })
    return {"count": len(repos), "repos": repos}


@mcp.tool()
def azdo_get_file(project: str, repo: str, path: str, branch: str = "refs/heads/main") -> Dict[str, Any]:
    """
    Fetch a file from a repo (text).

    Args:
      project: project name
      repo: repo name or id
      path: file path like /README.md
      branch: git ref, e.g. refs/heads/main
    """
    project_enc = urllib.parse.quote(project)
    repo_enc = urllib.parse.quote(repo)
    path_enc = urllib.parse.quote(path)

    # Items API
    url = (
        f"{_org_url()}/{project_enc}/_apis/git/repositories/{repo_enc}/items"
        f"?path={path_enc}&includeContent=true&versionDescriptor.versionType=branch"
        f"&versionDescriptor.version={urllib.parse.quote(branch.replace('refs/heads/',''))}"
        f"&api-version=7.1-preview.1"
    )
    data = _request("GET", url)
    content = data.get("content", "")
    return {"path": path, "branch": branch, "content": content}


def _get_repo(project: str, repo: str) -> Dict[str, Any]:
    project_enc = urllib.parse.quote(project)
    repo_enc = urllib.parse.quote(repo)
    url = _api(f"/{project_enc}/_apis/git/repositories/{repo_enc}", api_version="7.1-preview.1")
    return _request("GET", url)


def _get_ref_object_id(project: str, repo_id: str, ref_name: str) -> str:
    # ref_name must be like refs/heads/main
    project_enc = urllib.parse.quote(project)
    url = (
        f"{_org_url()}/{project_enc}/_apis/git/repositories/{repo_id}/refs"
        f"?filter={urllib.parse.quote(ref_name)}&api-version=7.1-preview.1"
    )
    data = _request("GET", url)
    vals = data.get("value", [])
    if not vals:
        raise RuntimeError(f"Ref not found: {ref_name}")
    return vals[0].get("objectId")


def _create_or_update_ref(project: str, repo_id: str, new_ref: str, old_object_id: str, new_object_id: str):
    project_enc = urllib.parse.quote(project)
    url = _api(f"/{project_enc}/_apis/git/repositories/{repo_id}/refs", api_version="7.1-preview.1")
    body = [{
        "name": new_ref,
        "oldObjectId": old_object_id,
        "newObjectId": new_object_id
    }]
    return _request("POST", url, json=body)


def _create_push(project: str, repo_id: str, ref_updates: List[Dict[str, Any]], commits: List[Dict[str, Any]]):
    project_enc = urllib.parse.quote(project)
    url = _api(f"/{project_enc}/_apis/git/repositories/{repo_id}/pushes", api_version="7.1-preview.2")
    body = {
        "refUpdates": ref_updates,
        "commits": commits
    }
    return _request("POST", url, json=body)


def _create_pr(project: str, repo_id: str, source_ref: str, target_ref: str, title: str, description: str = ""):
    project_enc = urllib.parse.quote(project)
    url = _api(f"/{project_enc}/_apis/git/repositories/{repo_id}/pullrequests", api_version="7.1-preview.1")
    body = {
        "sourceRefName": source_ref,
        "targetRefName": target_ref,
        "title": title,
        "description": description
    }
    return _request("POST", url, json=body)


@mcp.tool()
def azdo_update_file_and_create_pr(
    project: str,
    repo: str,
    file_path: str,
    new_content: str,
    pr_title: str,
    base_branch: str = "main",
    new_branch: str = "mcp/update-file",
    pr_description: str = "",
) -> Dict[str, Any]:
    """
    Update a single file in a repo by creating a new branch, pushing a commit, and opening a PR.

    Args:
      project: Azure DevOps project name
      repo: repo name or id
      file_path: e.g. /README.md
      new_content: full file contents (text)
      pr_title: PR title
      base_branch: e.g. main
      new_branch: e.g. feature/my-change
      pr_description: optional description
    """
    repo_obj = _get_repo(project, repo)
    repo_id = repo_obj["id"]

    base_ref = f"refs/heads/{base_branch}"
    source_ref = f"refs/heads/{new_branch}"

    base_object_id = _get_ref_object_id(project, repo_id, base_ref)

    # Create the branch ref pointing to base (if it doesn't exist)
    # If it exists, we will update it to base (safe default).
    try:
        existing_source_object_id = _get_ref_object_id(project, repo_id, source_ref)
        # Update branch to base (force) so it's deterministic
        _create_or_update_ref(project, repo_id, source_ref, existing_source_object_id, base_object_id)
    except Exception:
        # Create new ref (oldObjectId is all zeros)
        _create_or_update_ref(project, repo_id, source_ref, "0000000000000000000000000000000000000000", base_object_id)

    # Now push a commit on that branch updating the file
    push = _create_push(
        project,
        repo_id,
        ref_updates=[{
            "name": source_ref,
            "oldObjectId": base_object_id
        }],
        commits=[{
            "comment": pr_title,
            "changes": [{
                "changeType": "edit",
                "item": {"path": file_path},
                "newContent": {
                    "content": new_content,
                    "contentType": "rawtext"
                }
            }]
        }]
    )

    pr = _create_pr(
        project=project,
        repo_id=repo_id,
        source_ref=source_ref,
        target_ref=base_ref,
        title=pr_title,
        description=pr_description
    )

    return {
        "repo": {"id": repo_id, "name": repo_obj.get("name")},
        "baseRef": base_ref,
        "sourceRef": source_ref,
        "pushId": push.get("pushId"),
        "pullRequestId": pr.get("pullRequestId"),
        "prUrl": pr.get("url"),
        "webUrl": pr.get("_links", {}).get("web", {}).get("href"),
    }


def main():
    # stdio transport by default for Claude MCP
    mcp.run()


if __name__ == "__main__":
    main()
