# health-check-monitor

A single GitHub Actions workflow that pings the health-check endpoint of every
service you list, on a schedule, and emails you (via Mailgun) only when a
service goes down or recovers — no spam on every run.

## How it works

- [`.github/workflows/health-check.yml`](.github/workflows/health-check.yml) runs every 5 minutes (and on-demand via
  `workflow_dispatch`).
- It runs [`scripts/check_health.py`](scripts/check_health.py), which reads [`services.yml`](services.yml), does an HTTP
  GET against each `url`, and compares the result against the expected status
  code.
- The current status of every service is written to `state.json`. The
  workflow commits that file back to the repo only when it changes, so you
  get a small history of uptime/downtime transitions in `git log`.
- An email is sent through the [Mailgun HTTP API](https://documentation.mailgun.com/) only when a service's
  status *changes* (up → down or down → up) — not on every 5-minute run.
- If any service is currently down, the workflow run itself is marked
  failed, so you also get GitHub's own run-failure signal/notifications.

## Setup

1. **Add your services** in [`services.yml`](services.yml):

   ```yaml
   services:
     - name: My API
       url: https://api.example.com/health
       expected_status: 200
       timeout: 10
   ```

2. **Add repo secrets** (Settings → Secrets and variables → Actions):

   | Secret              | Required | Description                                              |
   | -------------------- | -------- | --------------------------------------------------------- |
   | `MAILGUN_API_KEY`    | yes      | Your Mailgun private API key                              |
   | `MAILGUN_DOMAIN`     | yes      | The Mailgun sending domain (e.g. `mg.yourdomain.com`)     |
   | `ALERT_EMAIL_TO`     | yes      | Where alerts should be sent                               |
   | `ALERT_EMAIL_FROM`   | no       | Defaults to `Health Check Monitor <alerts@$MAILGUN_DOMAIN>` |

   If secrets aren't set, the workflow still runs and logs results — it just
   skips sending email (with a warning) instead of failing.

3. **Push this repo to GitHub.** The workflow needs `contents: write`
   permission to commit `state.json` back — this is already requested in the
   workflow file and works with the default `GITHUB_TOKEN`, no extra PAT
   needed.

4. Optionally trigger a manual run from the Actions tab (`workflow_dispatch`)
   to verify everything before waiting for the schedule.

## Services that need credentials or a POST request

Some health-check endpoints require an auth token, basic auth, or a POST with
a body. `services.yml` supports this, but **never write the actual
credential value into `services.yml`** — it's committed to git. Instead:

1. In `services.yml`, reference a placeholder with `${VAR_NAME}`:

   ```yaml
   - name: Internal Admin API
     url: https://internal.example.com/health
     method: POST
     expected_status: 200
     headers:
       Authorization: "Bearer ${INTERNAL_API_TOKEN}"
     body:
       ping: true

   - name: Protected Service
     url: https://protected.example.com/health
     expected_status: 200
     auth:
       type: basic
       username: "${PROTECTED_SERVICE_USER}"
       password: "${PROTECTED_SERVICE_PASS}"
   ```

2. Add the real value as a repo secret (Settings → Secrets and variables →
   Actions), e.g. `INTERNAL_API_TOKEN`.

3. Map it into the `Run health checks` step's `env:` block in
   [`health-check.yml`](.github/workflows/health-check.yml):

   ```yaml
   INTERNAL_API_TOKEN: ${{ secrets.INTERNAL_API_TOKEN }}
   ```

Since the value flows through `secrets.*` into an env var, GitHub Actions
automatically redacts it from the workflow logs if it ever appears in output.
If `services.yml` references a `${VAR_NAME}` that isn't set, that service is
reported as `down` with a clear error instead of silently sending the literal
placeholder string.

Supported per-service fields beyond `name`/`url`/`expected_status`/`timeout`:

| Field     | Purpose                                                         |
| --------- | ---------------------------------------------------------------- |
| `method`  | HTTP method, defaults to `GET`                                   |
| `headers` | dict of request headers, values may use `${VAR_NAME}`            |
| `body`    | dict sent as JSON (for POST/PUT/etc.)                             |
| `auth`    | `{ type: basic, username: ${VAR}, password: ${VAR} }`             |

## Switching to Maileroo instead of Mailgun

The email logic lives entirely in `send_email()` in
[`scripts/check_health.py`](scripts/check_health.py). To use Maileroo instead, swap that function's
`requests.post(...)` call for Maileroo's send-email API endpoint and update
the secret names/workflow env accordingly.

## Notes on schedule accuracy

GitHub Actions `schedule` cron triggers are best-effort — under load, GitHub
may delay a run by several minutes. For most health-check use cases this is
fine; if you need guaranteed sub-minute accuracy, an Action-based cron isn't
the right tool.
