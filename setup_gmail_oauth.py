#!/usr/bin/env python3
"""
One-time script to obtain Gmail OAuth2 tokens with send-only scope.
Run this once locally, then add the printed values as GitHub secrets.

Scope granted: gmail.send ONLY — the app can never read your inbox.
"""

import json
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Installing required package...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "google-auth-oauthlib"])
    from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

print("""
================================================================
         Gmail OAuth2 Setup -- Send-Only Access
================================================================

This grants the digest SEND-ONLY access to Gmail.
Your inbox cannot be read, searched, or modified.

Before running this script you need:
  1. A Google Cloud project with Gmail API enabled
  2. OAuth2 credentials (client_secrets.json) downloaded

Steps:
  1. Go to https://console.cloud.google.com
  2. Create a new project (or use existing)
  3. APIs & Services -> Enable APIs -> search "Gmail API" -> Enable
  4. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
  5. Application type: Desktop app -> Create
  6. Download JSON -> save as client_secrets.json in this folder
  7. APIs & Services -> OAuth consent screen -> set to "Production"
     (this prevents the 7-day token expiry for testing apps)

Press Enter when client_secrets.json is ready...
""")
input()

try:
    with open("client_secrets.json") as f:
        secrets = json.load(f)
    client_id     = secrets["installed"]["client_id"]
    client_secret = secrets["installed"]["client_secret"]
except FileNotFoundError:
    print("ERROR: client_secrets.json not found in this directory.")
    sys.exit(1)
except KeyError:
    print("ERROR: client_secrets.json format unexpected. Download a fresh copy.")
    sys.exit(1)

print("Opening browser for authorization (you'll see an 'unverified app' warning).")
print("Click 'Advanced' → 'Go to [app name]' → Grant access.\n")

flow = InstalledAppFlow.from_client_secrets_file("client_secrets.json", SCOPES)
creds = flow.run_local_server(port=0)

print("""
================================================================
  Authorization successful! Add these as GitHub secrets:
================================================================

Go to: https://github.com/dizchrisctrl/daily-digest/settings/secrets/actions
""")
import os as _os

_secrets_file = "github_secrets.txt"
with open(_secrets_file, "w") as _f:
    _f.write(f"GMAIL_ADDRESS=<your Gmail address>\n")
    _f.write(f"GMAIL_CLIENT_ID={creds.client_id}\n")
    _f.write(f"GMAIL_CLIENT_SECRET={creds.client_secret}\n")
    _f.write(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}\n")

# Restrict file permissions so only the current user can read it
try:
    _os.chmod(_secrets_file, 0o600)
except Exception:
    pass

print(f"Secret name: GMAIL_ADDRESS")
print(f"Secret value: (your Gmail address)\n")
print(f"Secret name: GMAIL_CLIENT_ID")
print(f"Secret value: {creds.client_id[:6]}... (see {_secrets_file})\n")
print(f"Secret name: GMAIL_CLIENT_SECRET")
print(f"Secret value: *** (see {_secrets_file})\n")
print(f"Secret name: GMAIL_REFRESH_TOKEN")
print(f"Secret value: *** (see {_secrets_file})\n")
print(f"Full values written to {_secrets_file} (chmod 600).")
print("Delete that file once you have added the secrets to GitHub.")
print("After adding secrets, you can also delete client_secrets.json")
