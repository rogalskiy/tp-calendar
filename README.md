# TrainingPeaks → Google Calendar sync

Auto-syncs planned workouts from a **free** TrainingPeaks account to a Google Calendar, running every 4 hours on GitHub Actions.

## Architecture

```
GitHub Actions cron (4h)
  └─ Playwright logs into TP (passes reCAPTCHA v3)
      └─ Exchanges cookie for OAuth access token
          └─ Fetches planned workouts via tpapi.trainingpeaks.com
              └─ Diffs vs. existing synced events, create/update/delete
```

Events are tagged with a private `extendedProperty` so the script only ever touches events it created — your own calendar entries are safe.

On failure (auth drift, API change, empty response for the next 7 days) the script exits non-zero and GitHub emails you.

## One-time setup

### 1. Create the GitHub repo

```bash
cd ~/Downloads/tp-calendar-sync
git init
git add .
git commit -m "Initial sync scaffold"
# Create the repo on github.com (private!), then:
git remote add origin git@github.com:<you>/tp-calendar-sync.git
git push -u origin main
```

### 2. Google Cloud → OAuth credentials (free)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create a new project (e.g. `tp-calendar-sync`). **No billing needed.**
2. *APIs & Services → Library* → enable **Google Calendar API**.
3. *APIs & Services → OAuth consent screen* → User type: **External** → fill the minimal fields → add yourself as a **Test user**. (You don't need to publish; test mode works indefinitely for the test user.)
4. *APIs & Services → Credentials → Create credentials → OAuth client ID* → Application type: **Desktop app** → download the JSON, save as `client_secret.json` next to `get_google_token.py`.
5. Mint the refresh token locally:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python get_google_token.py
   ```
   A browser opens; grant access. The script prints a JSON blob — copy it.

### 3. GitHub Secrets

In your repo: *Settings → Secrets and variables → Actions → New repository secret*:

| Name                      | Value                                                      |
| ------------------------- | ---------------------------------------------------------- |
| `TP_USERNAME`             | `rogalskiy`                                                |
| `TP_PASSWORD`             | your TrainingPeaks password                                |
| `GOOGLE_CREDENTIALS_JSON` | the JSON blob printed by `get_google_token.py`             |

### 4. (Optional) GitHub Variables

Under *Settings → Secrets and variables → Actions → Variables*:

| Name                 | Default          | Purpose                                     |
| -------------------- | ---------------- | ------------------------------------------- |
| `GOOGLE_CALENDAR_ID` | `primary`        | Target calendar (use a dedicated one if you want the workouts in a separate color) |
| `TIMEZONE`           | `Europe/Warsaw`  | Event timezone                               |
| `SYNC_DAYS`          | `14`             | Days ahead to sync                           |

**Recommended:** create a dedicated calendar ("Training") in Google Calendar, find its ID under *Calendar settings → Integrate calendar → Calendar ID*, and put that in `GOOGLE_CALENDAR_ID`. That way workouts appear in their own color and can be toggled off independently.

### 5. First run

- Go to *Actions → TrainingPeaks → Google Calendar sync → Run workflow*.
- Watch the logs. You should see `Sync summary: N created, …`.
- Check Google Calendar — workouts should appear at 07:00 local on each planned day.

## Troubleshooting

- **Login failed**: verify `TP_USERNAME` / `TP_PASSWORD` secrets. TP has no 2FA so a plain login should work.
- **Health check failed**: the API returned 0 workouts for the next 7 days. Log in manually to check your TP calendar is actually populated; if it is, TP's internal API may have changed.
- **`invalid_grant` from Google**: your refresh token was revoked. Re-run `get_google_token.py` and update the secret.
- **reCAPTCHA timeout**: Google updated their anti-bot; add `playwright-stealth` or pin a specific Chromium version.

## Testing locally

```bash
export TP_USERNAME=... TP_PASSWORD=... \
       GOOGLE_CREDENTIALS_JSON='{"client_id":"...","client_secret":"...","refresh_token":"..."}' \
       DRY_RUN=true
python sync.py
```

`DRY_RUN=true` logs what it *would* do without touching your calendar.
