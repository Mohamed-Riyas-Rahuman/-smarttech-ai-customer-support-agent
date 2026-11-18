"""Gmail OAuth2 Interactive Authorization Script for Desktop Application."""
import json
import sys
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
CREDS_FILE = BACKEND_DIR / "secrets" / "gmail_credentials.json"
TOKEN_FILE = BACKEND_DIR / "secrets" / "gmail_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def main():
    print("=" * 60)
    print("SmartTech Support Agent - Gmail OAuth Authorization")
    print("=" * 60)

    if not CREDS_FILE.is_file():
        print(f"[ERROR] Credentials file not found: {CREDS_FILE}")
        sys.exit(1)

    print(f"[OK] Found OAuth client credentials at: {CREDS_FILE}")
    print(f"[INFO] Requesting scopes: {SCOPES}")

    # Check if token already exists
    if TOKEN_FILE.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
            if creds and creds.valid:
                print(f"[OK] Token already exists and is valid at: {TOKEN_FILE}")
                return
            elif creds and creds.expired and creds.refresh_token:
                print("[INFO] Token is expired, attempting refresh...")
                creds.refresh(Request())
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                print(f"[OK] Refreshed token saved to: {TOKEN_FILE}")
                return
        except Exception as e:
            print(f"[WARNING] Existing token invalid or failed to refresh: {e}")

    print("\nStarting local OAuth flow. A browser window will open for authorization...")
    print("If browser does not open automatically, copy the URL displayed and open it manually.\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), scopes=SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

        print(f"\n[SUCCESS] Gmail authorization complete! Token saved to: {TOKEN_FILE}")
    except Exception as e:
        print(f"\n[ERROR] Authorization failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
