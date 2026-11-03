# AI Job Radar

Finds new AI/ML jobs for an entry-level candidate every hour, filters and scores them against a
personal profile, screens them for fraud, and sends each relevant opening **once** to Telegram.
Runs on GitHub Actions with a Neon Postgres database, so no laptop needs to stay on.

```
Sources ──► Collectors ──► Filters ──► Duplicate detection ──► Scoring ──► Verification ──► Telegram
(YC, company     (per source,   (title, experience,   (same opening on      (skills, similarity,   (VERIFIED /          (alerts, daily
 job boards)      retries)       location, age)        several sites)        experience, salary)    NEEDS_REVIEW /       digest, bot
                                                                                                    SUSPICIOUS)          commands)
```

## What it does

| Stage | Behaviour |
|---|---|
| **Sources** | Y Combinator job pages (public, allowed by robots.txt) and the official public job-board APIs of companies on Greenhouse, Lever and Ashby. Job boards of YC companies are discovered automatically. |
| **Filters** | Keeps AI/ML roles open to freshers (≤ 2 years required), in India, remote-for-India, or abroad with visa sponsorship. Skips senior roles, non-AI roles, part-time, and postings older than 30 days. Abroad with sponsorship unknown goes to the daily digest only. |
| **Duplicates** | The same opening on YC and on the company's board becomes one job with both links. The same role in several cities is sent as one message. |
| **Scoring (0–100)** | Weighted skill matches from your profile, semantic similarity to your profile summary (small local embedding model, no LLM, no API costs), title, experience, location, salary vs ₹4.5 LPA, recency; penalties for PhD requirements, senior pay bands, other graduation years. |
| **Verification** | VERIFIED: on the company's own confirmed job board. SUSPICIOUS: asks for fees/deposits, or applying via WhatsApp/Telegram. NEEDS_REVIEW: not enough evidence either way. Evidence is stored with every job. These are heuristics, not proof. |
| **Telegram** | Alerts for jobs scoring ≥ 60 (max 10 per run, best first), a daily digest after 20:00 IST with jobs not alerted, and `/latest`, `/stats`, `/preferences`, `/help`. A job is never sent twice. |

### Sources: what is and isn't covered

| Source | Status | Notes |
|---|---|---|
| Y Combinator (ycombinator.com/jobs) | ✅ | Public role pages show the latest ~40 postings each; applying needs a YC account. |
| Company boards (Greenhouse, Lever, Ashby) | ✅ | Official public APIs, no login. 37 companies configured in `config/sources.yaml` plus discovered YC company boards. |
| Indeed | ❌ skipped | Search pages are behind Cloudflare CAPTCHA and the public Publisher API has been retired. Bypassing protections is not done. |
| Wellfound | ❌ skipped | Protected by DataDome anti-bot; terms not reviewed. |

## Local setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in the values below
```

1. **Create the bot:** in Telegram, message @BotFather → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN` in `.env`.
   Never paste the token anywhere else. If it is ever exposed, send `/revoke` to @BotFather and use the new one.
2. **Check it:** `.venv/bin/python -m scripts.telegram_setup check`
3. **Register your chat:** press *Start* in your bot, then `.venv/bin/python -m scripts.telegram_setup register` and put
   the printed `TELEGRAM_CHAT_ID` into `.env`.
4. **Test message:** `.venv/bin/python -m scripts.telegram_setup test`
5. **One full run:** `.venv/bin/python main.py` (about 3 minutes; uses local SQLite at `data/job_radar.db` unless
   `DATABASE_URL` is set).

## Deploy (hourly, in the cloud)

### 1. Database: Neon (free)
1. Sign up at [neon.tech](https://neon.tech) and create a project. **Choose region AWS US East (N. Virginia)**,
   close to GitHub's runners, so each database round trip is fast.
2. Copy the connection string (`postgresql://user:password@host/dbname?sslmode=require`). Treat it like a password.

### 2. Copy your existing jobs into Neon (once)
This keeps everything already found and **already sent**, so nothing is re-sent after the move:
```bash
.venv/bin/python -m scripts.migrate_database --source sqlite:///data/job_radar.db --target "postgresql://...your Neon URL..."
```
It refuses to write into a database that already has jobs, and prints a row-count check for every table.

### 3. GitHub repository
1. Create a **private** repository on github.com (private keeps your profile and preferences private;
   hourly runs fit in the free private-repo minutes, see costs below).
2. Push this project (the `.gitignore` excludes `.env`, its backups, `data/` and resumes):
   ```bash
   git init && git add . && git commit -m "AI Job Radar"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. In the repository: **Settings → Secrets and variables → Actions → New repository secret**, add:

   | Secret | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | the bot token |
   | `TELEGRAM_CHAT_ID` | your chat id |
   | `DATABASE_URL` | the Neon connection string |

4. **Actions** tab → *Job monitor* → **Run workflow** once to check everything works. After that it runs by
   itself every hour (at minute 23, UTC).

The *Tests* workflow runs the test suite on SQLite and PostgreSQL on every push.

## Configuration

Both files are plain YAML; changes apply on the next run (stored jobs are re-evaluated automatically).

- **`config/job_preferences.yaml`**: title terms, experience limit, locations and visa rules, employment types,
  maximum posting age, your skill profile (weights 1–3) and profile summary, scoring weights and penalties,
  alert threshold, digest time, alerts per run.
- **`config/sources.yaml`**: YC pages, company boards (`{ats, slug, company}`), request rates, YC board discovery.

To add a company board, find its slug in the board URL (`jobs.ashbyhq.com/<slug>`,
`boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`) and add a line to `config/sources.yaml`.

## Useful commands (offline unless noted)

| Command | Purpose |
|---|---|
| `python main.py` | One full run (collects live) |
| `python -m scripts.show_jobs [--status passed\|review\|rejected] [--explain]` | Stored jobs, best first, with reasons |
| `python -m scripts.show_jobs --reasons` | Why jobs were rejected, counted |
| `python -m scripts.reevaluate` | Re-apply preferences and re-score all stored jobs |
| `python -m scripts.verify_jobs [--discover N]` | Verification results (`--discover` makes a few live requests) |
| `python -m scripts.notify --dry-run` | Show which alerts would be sent |
| `python -m scripts.notify` | Answer bot commands and send pending alerts/digest now |
| `python -m scripts.collect_preview` | Live collection summary without storing |
| `python -m pytest -q` | Tests (`TEST_DATABASE_URL=postgresql://...` to run them on Postgres) |

Prefix with `.venv/bin/` locally. Set `DATABASE_URL` to run any of them against Neon.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| No alerts for a while | Normal when nothing new qualifies; check `/stats`. Alerts need score ≥ 60, a posting ≤ 30 days old, and not already sent. Run `scripts.notify --dry-run` locally with `DATABASE_URL` set to see pending alerts. |
| Bot doesn't answer commands | Commands are answered on the next hourly run (no always-on server). Only the chat in `TELEGRAM_CHAT_ID` is answered. |
| `TELEGRAM_BOT_TOKEN is not set` / 401 Unauthorized | Missing or revoked token: update `.env` locally or the GitHub secret. |
| `Forbidden: bot was blocked by the user` | Unblock the bot in Telegram; sending resumes next run (failed alerts are retried for 24 h). |
| Workflow runs late or skips an hour | GitHub runs schedules best effort; delays happen under load. Use *Run workflow* for a manual run. |
| A source shows `ok: false` in the run log | That source's site changed or was down; other sources still run. Check the Actions log for the error. |
| `Semantic model ... unavailable` warning | The embedding model could not load; scoring continues with rules only. Usually fixed on the next run. |
| Database connection errors | Check `DATABASE_URL` (must include `sslmode=require` for Neon). Neon suspends idle databases; the first query wakes it in about a second. |
| GitHub minutes running low | See *Settings → Billing*. Reduce frequency in `.github/workflows/job_monitor.yml` (e.g. `23 */2 * * *` for every two hours). |

Run history is stored in the database (`pipeline_runs`, `collector_runs`) and in the Actions logs.

## Cost

| Item | Plan | Expected use | Cost |
|---|---|---|---|
| GitHub Actions | Free plan: 2,000 min/month for private repos (public repos: unlimited) | ~720 runs × ~2 billed min (each run is rounded up to whole minutes) ≈ 1,440 min, plus a few for tests on push | ₹0 |
| Neon Postgres | Free plan (0.5–1 GB storage; tens of compute-hours per month) | Tens of MB; ~20 compute-hours (each run wakes the database for a few minutes) | ₹0 |
| Telegram Bot API | Free | A few messages per day | ₹0 |
| Semantic model | Runs locally (model2vec, ~30 MB, cached) | No API calls | ₹0 |
| **Total** | | | **₹0 / month** |

Free-tier limits change over time; check GitHub's and Neon's pricing pages. If the GitHub minutes ever run out,
runs stop until the next month (no charge without a payment method). Switching the repository to public would
make minutes unlimited, but your profile and preferences would become public.

## Security

- Secrets live only in `.env` (git-ignored, including editor backups like `.env.save`) and GitHub Secrets.
- The bot answers only the registered chat; job text is HTML-escaped and only `http(s)` links are rendered.
- Job data is treated as untrusted; board-discovery requests only use sanitized slugs on fixed API hosts.
- No contact details from the resume are stored; the profile in `job_preferences.yaml` contains skills and
  interests only.

## Moving off GitHub Actions later

Everything runs through `python main.py` with three environment variables, so any scheduler works: a VPS with
cron (`23 * * * * cd /path && .venv/bin/python main.py`), a cloud scheduler, etc. Collectors don't change.
