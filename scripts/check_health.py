#!/usr/bin/env python3
"""Check configured service endpoints and email an alert on status changes."""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
SERVICES_FILE = ROOT / "services.yml"
STATE_FILE = ROOT / "state.json"

SECRET_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_secrets(value):
    """Recursively replace ${ENV_VAR} placeholders with environment values.

    Lets services.yml reference credentials (auth headers, tokens, basic
    auth) by name without ever committing the real secret to the repo. The
    actual value must be provided as a GitHub secret and mapped into the
    workflow's env block.
    """
    if isinstance(value, str):
        def replace(match):
            var_name = match.group(1)
            resolved = os.environ.get(var_name)
            if resolved is None:
                raise ValueError(
                    f"services.yml references ${{{var_name}}} but no environment "
                    f"variable {var_name} is set (add it as a repo secret and map "
                    f"it in the workflow's env block)"
                )
            return resolved

        return SECRET_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: resolve_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secrets(v) for v in value]
    return value


def load_services():
    with open(SERVICES_FILE) as f:
        config = yaml.safe_load(f) or {}
    return config.get("services", [])


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def check_service(service):
    url = service["url"]
    method = service.get("method", "GET").upper()
    expected_status = service.get("expected_status", 200)
    timeout = service.get("timeout", 10)

    try:
        headers = resolve_secrets(service.get("headers", {}))
        body = resolve_secrets(service.get("body"))

        auth = None
        auth_config = service.get("auth")
        if auth_config and auth_config.get("type") == "basic":
            auth = (
                resolve_secrets(auth_config["username"]),
                resolve_secrets(auth_config["password"]),
            )
    except ValueError as exc:
        return "down", str(exc)

    try:
        resp = requests.request(
            method,
            url,
            headers=headers or None,
            json=body,
            auth=auth,
            timeout=timeout,
        )
        if resp.status_code == expected_status:
            return "up", f"HTTP {resp.status_code}"
        return "down", f"expected HTTP {expected_status}, got HTTP {resp.status_code}"
    except requests.RequestException as exc:
        return "down", str(exc)


def send_email(subject, body):
    api_key = os.environ.get("MAILGUN_API_KEY")
    domain = os.environ.get("MAILGUN_DOMAIN")
    to_addr = os.environ.get("ALERT_EMAIL_TO")
    from_addr = os.environ.get("ALERT_EMAIL_FROM") or f"Health Check Monitor <alerts@{domain}>"

    if not all([api_key, domain, to_addr]):
        print("::warning::Mailgun secrets not fully configured, skipping email alert")
        return

    resp = requests.post(
        f"https://api.mailgun.net/v3/{domain}/messages",
        auth=("api", api_key),
        data={
            "from": from_addr,
            "to": to_addr,
            "subject": subject,
            "text": body,
        },
        timeout=15,
    )
    resp.raise_for_status()
    print(f"Alert email sent: {subject}")


def main():
    services = load_services()
    if not services:
        print("No services configured in services.yml")
        return 0

    previous_state = load_state()
    current_state = {}
    transitions = []
    any_down = False

    for service in services:
        name = service["name"]
        status, detail = check_service(service)
        current_state[name] = {
            "status": status,
            "detail": detail,
            "last_checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if status == "down":
            any_down = True

        prev_status = previous_state.get(name, {}).get("status")
        if prev_status != status:
            transitions.append((name, prev_status, status, detail))

        print(f"[{status.upper()}] {name} - {detail}")

    save_state(current_state)

    if transitions:
        lines = []
        newly_down = []
        for name, prev, new, detail in transitions:
            if new == "down":
                lines.append(f"DOWN: {name} - {detail}")
                newly_down.append(name)
            else:
                lines.append(f"RECOVERED: {name} - {detail}")

        subject = (
            f"[ALERT] {len(newly_down)} service(s) down"
            if newly_down
            else "[RESOLVED] All services recovered"
        )
        send_email(subject, "\n".join(lines))

    return 1 if any_down else 0


if __name__ == "__main__":
    sys.exit(main())
