"""Provision a private, disposable GitHub test target using an existing account.

Credentials stay in memory and are only sent to api.github.com. This script
does not push product code, invite users, or change an existing repository.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

import httpx


def provision(root, account):
    manifest = Path(root).resolve() / "targets.json"
    data = json.loads(manifest.read_text(encoding="utf8"))
    description = "Disposable acceptance target for Gitgo Issue 40; synthetic fixtures only."
    name = "gitgo-issue40-acceptance-" + hashlib.sha256(str(manifest.parent).encode()).hexdigest()[:10]
    env = {**os.environ, "GCM_INTERACTIVE": "Never", "GIT_TERMINAL_PROMPT": "0"}
    credential = subprocess.run(["git", "credential-manager", "get"], input="protocol=https\nhost=github.com\nusername=" + account + "\n\n",
        capture_output=True, text=True, encoding="utf8", env=env, timeout=30)
    fields = dict(row.split("=", 1) for row in credential.stdout.splitlines() if "=" in row)
    token = fields.get("password")
    if credential.returncode or not token:
        raise ValueError("No existing noninteractive GitHub credential is available")
    with httpx.Client(base_url="https://api.github.com", timeout=30, follow_redirects=False,
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "gitgo-issue40-acceptance"}) as client:
        identity = client.get("/user")
        if identity.status_code != 200 or identity.json().get("login", "").casefold() != account.casefold():
            raise ValueError("Authenticated GitHub identity was not verified")
        response = client.get("/repos/" + account + "/" + name)
        if response.status_code == 404:
            if data.get("github", {}).get("state") == "creation_intent":
                raise ValueError("Prior create has an unknown result; inspect GitHub before another write")
            data["github"] = {"state": "creation_intent", "account": account, "repository": account + "/" + name}
            manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
            response = client.post("/user/repos", json={"name": name, "description": description,
                "private": True, "auto_init": False, "has_issues": True, "has_wiki": False})
        if response.status_code not in (200, 201):
            raise ValueError("GitHub target unavailable: HTTP " + str(response.status_code))
        repo = response.json()
        if repo.get("private") is not True or repo.get("description") != description or repo.get("owner", {}).get("login", "").casefold() != account.casefold():
            raise ValueError("An existing repository does not match the disposable target")
        data["github"] = {"state": "ready", "account": account, "repository": repo["full_name"],
            "repository_id": repo["id"], "url": repo["html_url"], "clone_url": repo["clone_url"],
            "private": True, "second_account_verified": False}
        manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
        print(json.dumps(data["github"], ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--account", required=True)
    args = parser.parse_args()
    try:
        provision(args.root, args.account)
    except Exception as exc:
        # Never format HTTP request objects or credential-helper output.
        print(json.dumps({"state": "unavailable", "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, ValueError) else "Target provisioning did not complete"}))
        raise SystemExit(1)
