# ============================================================
# KOTAK SCRIP MASTER DOWNLOADER — CORRECT AUTH FLOW
# Based on your ZIP auth.py + option_manager.py logic
# ============================================================

import os
import sys
import csv
import io
import requests
from datetime import datetime

try:
    import pyotp
    from neo_api_client import NeoAPI
except ImportError as e:
    print("Missing package:", e)
    print("\nInstall these first:")
    print("pip install pyotp requests neo_api_client")
    sys.exit(1)


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def generate_totp(secret_key: str):
    if not secret_key or secret_key == "YOUR_TOTP_SECRET_KEY":
        return None
    try:
        key = secret_key.upper().strip().replace(" ", "")
        padding = (8 - len(key) % 8) % 8
        key = key + "=" * padding
        return pyotp.TOTP(key).now()
    except Exception as e:
        print("TOTP auto-generation failed:", e)
        return None


def get_client():
    load_env()

    consumer_key = os.getenv("KOTAK_CONSUMER_KEY", "")
    mobile_number = os.getenv("KOTAK_MOBILE_NUMBER", "")
    ucc = os.getenv("KOTAK_UCC", "")
    mpin = os.getenv("KOTAK_MPIN", "")
    environment = os.getenv("KOTAK_ENVIRONMENT", "prod")
    totp_secret = os.getenv("TOTP_SECRET_KEY", "")

    if not consumer_key:
        raise ValueError("KOTAK_CONSUMER_KEY missing in .env")
    if not mobile_number:
        raise ValueError("KOTAK_MOBILE_NUMBER missing in .env")
    if not ucc:
        raise ValueError("KOTAK_UCC missing in .env")
    if not mpin:
        raise ValueError("KOTAK_MPIN missing in .env")

    print("=" * 55)
    print("Connecting to Kotak Neo API...")
    print("=" * 55)

    client = NeoAPI(
        consumer_key=consumer_key,
        environment=environment,
        access_token=None,
        neo_fin_key=None,
    )

    # Step 1: TOTP login
    totp_code = generate_totp(totp_secret)
    if totp_code:
        print("Auto-generated TOTP:", totp_code)
    else:
        totp_code = input("Enter 6-digit TOTP: ").strip()

    try:
        login_response = client.totp_login(
            mobilenumber=mobile_number,
            ucc=ucc,
            totp=totp_code
        )
    except Exception:
        login_response = client.totp_login(
            mobile_number=mobile_number,
            ucc=ucc,
            totp=totp_code
        )

    if not login_response:
        raise RuntimeError("TOTP login failed / empty response")

    print("TOTP login: SUCCESS")

    # Step 2: MPIN validate  ← THIS WAS MISSING IN YOUR PREVIOUS FILE
    print("Validating MPIN...")
    validate_response = None

    try:
        validate_response = client.totp_validate(mpin=mpin)
    except Exception:
        try:
            validate_response = client.totp_validate(mpin=mpin, pan=ucc)
        except Exception:
            auth = login_response.get("token", "") if isinstance(login_response, dict) else ""
            sid = login_response.get("sid", "") if isinstance(login_response, dict) else ""
            validate_response = client.totp_validate(mpin=mpin, auth=auth, sid=sid)

    if isinstance(validate_response, dict) and validate_response.get("error"):
        raise RuntimeError(f"MPIN validation failed: {validate_response}")

    print("MPIN validated: SUCCESS")
    print("Kotak login complete")

    return client


def save_scrip_master(exchange_segment="nse_fo", save_dir="."):
    client = get_client()

    print(f"Getting scrip master URL for: {exchange_segment}")
    url = client.scrip_master(exchange_segment=exchange_segment)

    if isinstance(url, dict):
        if "data" in url and isinstance(url["data"], str):
            url = url["data"]
        elif "url" in url and isinstance(url["url"], str):
            url = url["url"]
        else:
            raise RuntimeError(f"Unexpected scrip_master response: {url}")

    if not isinstance(url, str) or not url.startswith("http"):
        raise RuntimeError(f"Unexpected scrip_master response: {url}")

    print("Download URL received")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    text = resp.text
    rows = list(csv.DictReader(io.StringIO(text)))

    os.makedirs(save_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = os.path.join(save_dir, f"scrip_master_{exchange_segment}_{ts}.csv")

    with open(file_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    print(f"Rows downloaded: {len(rows)}")
    print("Saved file:")
    print(file_path)
    return file_path


if __name__ == "__main__":
    try:
        # change nse_fo to nse_cm if needed
        save_scrip_master(exchange_segment="nse_fo", save_dir=".")
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
