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
- The workflow run itself always completes as successful, even when a
  service is down — email is the only alerting signal, so the Actions
  history doesn't fill up with red X's for a known, ongoing outage.

## Setup

1. **Add your services** in [`services.yml`](services.yml):

   ```yaml
   services:
     - name: My API
       url: https://api.example.com/health
       expected_status: 200
       timeout: 10
   ```

   `expected_status` can be a single code, a list of acceptable codes (e.g.
   `[200, 202, 204]`), or omitted entirely to accept any 2xx response as
   success.

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

GitHub Actions `schedule` cron triggers are best-effort, and in practice the
gap can be much larger than the configured interval — this repo is set to
`*/5 * * * *` but has actually been observed running roughly **every ~2
hours**, not every 5 minutes. This isn't a config bug on our side; GitHub
does not reliably honor sub-hour `schedule` intervals for public/free-tier
repos, and appears to silently drop most ticks under system load rather than
running them all with delay.

If you ever need closer to the configured cadence, the fix isn't tuning the
cron expression further — it's bypassing GitHub's `schedule` trigger
entirely and having an external cron service (e.g. cron-job.org) call the
`workflow_dispatch` REST API on its own schedule instead.
`workflow_dispatch` has fired instantly and reliably every time it's been
tested manually, unlike `schedule`.

## Why Arcade / Chhakkadi / Zyvora aren't checked from here

These three are deliberately absent from `services.yml` (see the comment
block there). All three are proxied through Cloudflare (orange-cloud DNS
records pointing at Render), unlike WatchTower, which is DNS-only and never
touches Cloudflare at all. Cloudflare's **Bot Fight Mode** challenges traffic
from GitHub Actions' runner IPs (Microsoft Azure ASN) before a WAF custom
rule ever gets a chance to run — confirmed directly via Cloudflare's
Security Events log, which showed `Service: Bot fight mode, Action taken:
Managed Challenge` for our request, versus `Service: Custom rules, Action
taken: Skip` for an identical request from cron-job.org (Hetzner ASN, which
Cloudflare doesn't challenge as aggressively). On the Free plan, Bot Fight
Mode can't be selectively bypassed per-path — only Super Bot Fight Mode
(a paid feature) supports real allowlisting for challenged traffic.

Rather than disable Bot Fight Mode zone-wide, these three are monitored
directly via **cron-job.org** instead, since its traffic is proven to pass
cleanly. Point cron-job.org's own failure notifications at the same
`ALERT_EMAIL_TO` address used by this workflow's Mailgun alerts, so
everything still lands in one inbox even though the checks run in two
different places.

## Dashboard

[`docs/index.html`](docs/index.html) is a static, single-page dashboard (no build step) meant to be served via
**GitHub Pages**. It talks directly to the GitHub REST API from the browser:

- **Read-only, no token needed**: current status per service, recent workflow
  run history.
- **With a token**: add/edit/delete services in `services.yml`, trigger a
  manual run, and add/update/delete repo secrets (encrypted client-side with
  libsodium before they ever leave your browser — GitHub only ever sees the
  encrypted value). Adding a secret also auto-inserts its
  `NAME: ${{ secrets.NAME }}` line into the workflow's env block for you.

**Enable it**: Settings → Pages → Source: "Deploy from a branch" → Branch:
`main`, folder: `/docs` → Save. It'll be live at
`https://vijevira.github.io/cron-jobs/` a minute or two later.

**To edit**, create a token at
[github.com/settings/tokens?type=beta](https://github.com/settings/tokens?type=beta),
scoped only to the `cron-jobs` repo, with **Contents**, **Actions**, and
**Secrets** permissions set to Read and write. Paste it into the dashboard's
token field — it's saved only in that browser's `localStorage` and sent only
to `api.github.com`.

Saving a service through the dashboard **regenerates the whole
`services.yml` file** from its parsed data — the standard header comment is
re-added automatically, but any hand-written inline comments or
commented-out examples you'd added directly in the file will not survive a
dashboard save.

## Actions minutes and repo visibility

A 5-minute schedule runs roughly 8,600 times a month, and GitHub bills each
job in whole-minute increments — so this easily uses **8,000+ Actions
minutes/month**.

- **Public repos**: Actions minutes on standard runners are free/unlimited
  (subject to fair-use limits, a 6-hour per-job cap, and a 20-concurrent-job
  cap on the Free plan). This repo is meant to be public for that reason.
- **Private repos** only get 2,000 free minutes/month on the GitHub Free
  plan — a 5-minute schedule would blow past that. If you ever make this
  repo private, either raise the cron interval to ~30+ minutes or be ready
  to pay for the extra minutes.

## Why there's a heartbeat commit

GitHub automatically disables a scheduled workflow after **60 days with no
repository activity** (commits/pushes). Since `state.json` is only committed
when a service's status *changes*, a repo where every service stays healthy
for two straight months would go quiet and its schedule would silently stop
firing. To prevent that, the `Commit updated state` step in
[`health-check.yml`](.github/workflows/health-check.yml) pushes an empty
`chore: heartbeat...` commit whenever 7+ days have passed since the last
commit — well inside the 60-day window, with margin for a few failed or
skipped runs.
