"""Refresh Dhan access token using TOTP and update the GitHub repo secret.

Runs once a day (via a scheduled workflow). Flow:
  1. Generate current TOTP code from DHAN_TOTP_SECRET
  2. POST to Dhan auth endpoint with clientId + pin + totp
  3. Receive new accessToken (valid 24 h)
  4. Encrypt token with the repo's libsodium public key
  5. PUT the encrypted value as DHAN_ACCESS_TOKEN repo secret

After this, the main scanner workflow's reads of secrets.DHAN_ACCESS_TOKEN
will see the freshly-rotated token.

Required environment variables (set in the workflow as secrets):
  DHAN_CLIENT_ID    — your Dhan user ID (e.g., 1100123456)
  DHAN_PIN          — your Dhan trading PIN
  DHAN_TOTP_SECRET  — base32 TOTP secret from Dhan's "Set-up TOTP" page
  GH_REPO_PAT       — GitHub PAT with repo scope (for updating secrets)
  GH_OWNER          — GitHub username (auto-set in workflow)
  GH_REPO           — repo name (auto-set in workflow)
"""
from __future__ import annotations

import base64
import os
import sys

import pyotp
import requests
from nacl import encoding, public

DHAN_CLIENT_ID   = os.environ["DHAN_CLIENT_ID"]
DHAN_PIN         = os.environ["DHAN_PIN"]
DHAN_TOTP_SECRET = os.environ["DHAN_TOTP_SECRET"]
GH_PAT           = os.environ["GH_REPO_PAT"]
GH_OWNER         = os.environ["GH_OWNER"]
GH_REPO          = os.environ["GH_REPO"]


def generate_dhan_token() -> str:
    """Generate a new 24-hour Dhan access token via TOTP login.

    Security: TOTP code, PIN, and any error response body from Dhan are
    NEVER logged — they may contain credential echoes that would persist
    forever in public workflow logs.
    """
    totp_code = pyotp.TOTP(DHAN_TOTP_SECRET).now()
    print("  Computed TOTP (value redacted)")

    url = "https://auth.dhan.co/app/generateAccessToken"
    params = {
        "dhanClientId": DHAN_CLIENT_ID,
        "pin":          DHAN_PIN,
        "totp":         totp_code,
    }
    r = requests.post(url, params=params, timeout=15)
    if not r.ok:
        # Do NOT include r.text — may echo PIN or TOTP back to logs.
        raise RuntimeError(
            f"Dhan auth failed: HTTP {r.status_code} "
            f"(response body redacted to avoid leaking credentials)"
        )

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Dhan auth: non-JSON response (HTTP {r.status_code})")
    token = data.get("accessToken")
    if not token:
        raise RuntimeError("Dhan auth: response had no accessToken (body redacted)")
    expiry = data.get("expiryTime", "unknown")
    print(f"  ✓ Got token ({len(token)} chars), expires: {expiry}")
    return token


def update_github_secret(secret_name: str, secret_value: str) -> None:
    """Encrypt + write a repo secret via GitHub API."""
    base = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/actions/secrets"
    headers = {
        "Authorization":        f"Bearer {GH_PAT}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1. Fetch the repo's libsodium public key for secret encryption
    r = requests.get(f"{base}/public-key", headers=headers, timeout=10)
    r.raise_for_status()
    pk_data = r.json()
    public_key_b64 = pk_data["key"]
    key_id         = pk_data["key_id"]

    # 2. Encrypt the secret value using sealed box (libsodium)
    pub_key = public.PublicKey(public_key_b64.encode("utf-8"),
                               encoding.Base64Encoder())
    sealed  = public.SealedBox(pub_key).encrypt(secret_value.encode("utf-8"))
    encrypted_b64 = base64.b64encode(sealed).decode("utf-8")

    # 3. PUT the encrypted value
    r = requests.put(
        f"{base}/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted_b64, "key_id": key_id},
        timeout=10,
    )
    if r.status_code not in (201, 204):
        # Don't include r.text — GitHub API errors may include token snippets.
        raise RuntimeError(
            f"GitHub secret update failed: HTTP {r.status_code} (body redacted)"
        )
    print(f"  ✓ Updated repo secret '{secret_name}' (HTTP {r.status_code})")


def main() -> int:
    # Don't print client ID — even though it's quasi-public, no need to expose it.
    print(f"Refreshing Dhan token on {GH_OWNER}/{GH_REPO}")
    token = generate_dhan_token()

    # Tell GitHub Actions to mask this token in any subsequent logs
    # (belt + braces — GitHub auto-masks secret values, this ensures even
    # the freshly-generated string is masked before it's stored as a secret).
    print(f"::add-mask::{token}")

    update_github_secret("DHAN_ACCESS_TOKEN", token)
    print("Done. Next scanner run will use the new token.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
