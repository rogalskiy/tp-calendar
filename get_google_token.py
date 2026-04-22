"""One-off helper: run locally to mint a Google OAuth refresh token.

Steps:
  1. In Google Cloud Console, create an OAuth 2.0 Client ID of type
     "Desktop app" for your project (Calendar API enabled).
  2. Download the JSON as `client_secret.json` and place it next to this file.
  3. pip install google-auth-oauthlib google-auth
  4. python get_google_token.py
  5. A browser window opens → grant permission.
  6. The script prints a JSON blob. Copy it verbatim into the
     GOOGLE_CREDENTIALS_JSON GitHub Actions secret.

This only needs to be done ONCE. The refresh token does not expire under
normal use (unless you revoke access or rotate the client secret).
"""

from __future__ import annotations

import json
import pathlib

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CLIENT_SECRET_FILE = "client_secret.json"


def main() -> None:
    if not pathlib.Path(CLIENT_SECRET_FILE).exists():
        raise SystemExit(
            f"Place your OAuth client secret JSON at ./{CLIENT_SECRET_FILE} first."
        )
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    # `access_type=offline` + `prompt=consent` guarantees a refresh_token on
    # first consent AND on re-runs (Google otherwise omits it on repeat auth).
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )
    info = json.loads(creds.to_json())
    out = {
        "client_id": info["client_id"],
        "client_secret": info["client_secret"],
        "refresh_token": info["refresh_token"],
    }
    print("\n=== Copy this JSON into GOOGLE_CREDENTIALS_JSON secret ===\n")
    print(json.dumps(out, indent=2))
    print("\n=== End ===\n")


if __name__ == "__main__":
    main()
