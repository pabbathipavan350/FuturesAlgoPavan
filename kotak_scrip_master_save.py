import os
import csv
import io
import sys
from datetime import datetime

import requests

try:
    import pyotp
    from neo_api_client import NeoAPI
except ImportError as e:
    print("Missing package:", e)
    print("Install first:")
    print("pip install pyotp")
    print("pip install neo_api_client")
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
    if not secret_key:
        return None
    try:
        key = secret_key.upper().strip().replace(" ", "")
        padding = (8 - len(key) % 8) % 8
        key = key + "=" * padding
        return pyotp.TOTP(key).now()
    except Exception as e:
        print("TOTP generation failed:", e)
        return None


def get_client():
    load_env()

    consumer_key = os.getenv("KOTAK_CONSUMER_KEY", "")
    mobile_number = os.getenv("KOTAK_MOBILE_NUMBER", "")
    ucc = os.getenv("KOTAK_UCC", "")
    environment = os.getenv("KOTAK_ENVIRONMENT", "prod")
    totp_secret = os.getenv("TOTP_SECRET_KEY", "")

    if not consumer_key:
        raise ValueError("KOTAK_CONSUMER_KEY missing in .env")
    if not mobile_number:
        raise ValueError("KOTAK_MOBILE_NUMBER missing in .env")
    if not ucc:
        raise ValueError("KOTAK_UCC missing in .env")

    client = NeoAPI(
        consumer_key=consumer_key,
        environment=environment,
        access_token=None,
        neo_fin_key=None,
    )

    totp_code = generate_totp(totp_secret)
    if totp_code:
        print("Auto-generated TOTP:", totp_code)
    else:
        totp_code = input("Enter 6-digit TOTP: ").strip()

    login_response = None
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

    print("TOTP login response:", login_response)

    if not login_response:
        raise RuntimeError("TOTP login failed")

    return client


def download_scrip_master(exchange_segment="nse_fo", save_dir="."):
    client = get_client()

    print(f"Getting scrip master URL for segment: {exchange_segment}")
    url = client.scrip_master(exchange_segment=exchange_segment)

    if isinstance(url, dict):
        # some SDK versions may wrap data
        for key in ("data", "message", "url", "result"):
            if key in url:
                url = url[key]
                break

    if not isinstance(url, str) or not url.startswith("http"):
        raise RuntimeError(f"Unexpected scrip_master response: {url}")

    print("Download URL:", url)

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    text = resp.text
    rows = list(csv.DictReader(io.StringIO(text)))
    print("Rows downloaded:", len(rows))

    os.makedirs(save_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"scrip_master_{exchange_segment}_{timestamp}.csv"
    out_path = os.path.join(save_dir, out_name)

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    print("Saved successfully to:")
    print(out_path)
    return out_path


if __name__ == "__main__":
    try:
        # change "." to any folder path if you want
        download_scrip_master(exchange_segment="nse_fo", save_dir=".")
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
