"""Push metrics JSON to a private GitHub repo via Contents API."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any

import requests


def push_metrics_backup(
    data: dict[str, Any],
    *,
    path: str = "data/metrics.json",
) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    if not token or not repo or "/" not in repo:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPO (owner/name) are required for backup")

    owner, name = repo.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{name}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    raw = json.dumps(data, indent=2, ensure_ascii=False)
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")

    r = requests.get(url, headers=headers, timeout=30)
    sha = r.json().get("sha") if r.status_code == 200 else None

    msg = f"backup metrics {datetime.now(timezone.utc).isoformat()}"
    body: dict[str, Any] = {"message": msg, "content": b64}
    if sha:
        body["sha"] = sha

    put = requests.put(url, headers=headers, json=body, timeout=60)
    if put.status_code not in (200, 201):
        raise RuntimeError(f"GitHub backup failed: {put.status_code} {put.text[:500]}")
    return put.json()
