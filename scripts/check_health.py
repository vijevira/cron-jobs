#!/usr/bin/env python3
"""Check configured service endpoints and email an alert on status changes."""
import html
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

# Some WAFs/CDNs block the default "python-requests/x.x" user agent as a bot
# signature. A per-service `headers` entry for User-Agent still overrides this.
DEFAULT_HEADERS = {
    "User-Agent": "health-check-monitor/1.0 (+https://github.com/vijevira/cron-jobs)"
}


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


def status_ok(status_code, expected_status):
    """expected_status may be: omitted (any 2xx is success), a single code,
    or a list of acceptable codes."""
    if expected_status is None:
        return 200 <= status_code < 300, "2xx"
    if isinstance(expected_status, list):
        return status_code in expected_status, f"one of {expected_status}"
    return status_code == expected_status, str(expected_status)


def check_service(service):
    url = service["url"]
    method = service.get("method", "GET").upper()
    expected_status = service.get("expected_status")
    timeout = service.get("timeout", 10)

    try:
        headers = {**DEFAULT_HEADERS, **resolve_secrets(service.get("headers", {}))}
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
        ok, label = status_ok(resp.status_code, expected_status)
        if ok:
            return "up", f"HTTP {resp.status_code}"
        return "down", f"expected {label}, got HTTP {resp.status_code}"
    except requests.RequestException as exc:
        return "down", str(exc)


def build_alert_text(transitions):
    lines = []
    for name, _prev, new, detail in transitions:
        label = "DOWN" if new == "down" else "RECOVERED"
        lines.append(f"{label}: {name} - {detail}")
    return "\n".join(lines)


def _transition_row_html(name, new, detail):
    is_down = new == "down"
    label = "DOWN" if is_down else "RECOVERED"
    bg, fg = ("#fee2e2", "#991b1b") if is_down else ("#dcfce7", "#166534")
    return f"""
      <tr>
        <td style="padding:10px 14px;border-bottom:1px solid #f3f4f6;font-size:14px;color:#111827;">{html.escape(name)}</td>
        <td style="padding:10px 14px;border-bottom:1px solid #f3f4f6;">
          <span style="display:inline-block;padding:3px 10px;border-radius:999px;font-size:11px;
            font-weight:700;background:{bg};color:{fg};">{label}</span>
        </td>
        <td style="padding:10px 14px;border-bottom:1px solid #f3f4f6;font-size:13px;color:#4b5563;">{html.escape(detail)}</td>
      </tr>"""


def build_alert_html(subject, transitions):
    rows = "".join(_transition_row_html(name, new, detail) for name, _prev, new, detail in transitions)
    return f"""<!doctype html>
<html>
  <body style="margin:0;padding:24px;background:#f3f4f6;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;">
    <table role="presentation" width="100%" style="max-width:600px;margin:0 auto;background:#ffffff;
      border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;border-collapse:collapse;">
      <tr>
        <td style="padding:18px 24px;background:#111827;">
          <span style="color:#ffffff;font-size:15px;font-weight:700;">Health Check Monitor</span>
          <div style="color:#9ca3af;font-size:12px;margin-top:2px;">{html.escape(subject)}</div>
        </td>
      </tr>
      <tr>
        <td style="padding:0;">
          <table role="presentation" width="100%" style="border-collapse:collapse;">
            <tr>
              <th align="left" style="padding:10px 14px;background:#f9fafb;border-bottom:1px solid #e5e7eb;
                font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;">Service</th>
              <th align="left" style="padding:10px 14px;background:#f9fafb;border-bottom:1px solid #e5e7eb;
                font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;">Status</th>
              <th align="left" style="padding:10px 14px;background:#f9fafb;border-bottom:1px solid #e5e7eb;
                font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#6b7280;">Detail</th>
            </tr>
            {rows}
          </table>
        </td>
      </tr>
      <tr>
        <td style="padding:14px 24px;font-size:11px;color:#9ca3af;">
          Sent by the health-check-monitor scheduled workflow.
        </td>
      </tr>
    </table>
  </body>
</html>"""


def send_email(subject, transitions):
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
            "text": build_alert_text(transitions),
            "html": build_alert_html(subject, transitions),
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
        newly_down = [name for name, _prev, new, _detail in transitions if new == "down"]
        subject = (
            f"[ALERT] {len(newly_down)} service(s) down"
            if newly_down
            else "[RESOLVED] All services recovered"
        )
        send_email(subject, transitions)

    return 1 if any_down else 0


if __name__ == "__main__":
    sys.exit(main())
