#!/usr/bin/env python3
"""Check configured service endpoints and email an alert on status changes."""
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
SERVICES_FILE = ROOT / "services.yml"
STATE_FILE = ROOT / "state.json"


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
    expected_status = service.get("expected_status", 200)
    timeout = service.get("timeout", 10)
    try:
        resp = requests.get(url, timeout=timeout)
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
