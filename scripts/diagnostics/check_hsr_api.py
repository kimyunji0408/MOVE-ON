from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - keeps the script usable without dotenv.
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

HSR_API_KEY_ENV = "LADER_HSR"
HSR_ENDPOINT = "https://apihub.kma.go.kr/api/typ01/cgi-bin/url/nph-rdr_cmp1_api"


def load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(ENV_FILE)
        return

    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_hsr_key() -> str:
    hsr_key = os.getenv(HSR_API_KEY_ENV, "").strip()
    print(f"HSR key loaded: {bool(hsr_key)}")
    print(f"HSR key length: {len(hsr_key)}")

    if not hsr_key:
        raise RuntimeError("LADER_HSR 환경변수가 없습니다.")

    return hsr_key


def hsr_request_time(now: datetime | None = None) -> str:
    # HSR is produced every 5 minutes. Use KST, subtract 10 minutes for radar
    # production delay, then floor to the previous 5-minute boundary.
    current = now or datetime.now(ZoneInfo("Asia/Seoul"))
    delayed = current - timedelta(minutes=10)
    floored_minute = delayed.minute - (delayed.minute % 5)
    request_dt = delayed.replace(minute=floored_minute, second=0, microsecond=0)
    return request_dt.strftime("%Y%m%d%H%M")


def redact_key(text: str, secret: str) -> str:
    sanitized = text
    if secret:
        sanitized = sanitized.replace(secret, "{REDACTED}")
    sanitized = re.sub(
        r"([?&]authKey=)[^&\s]+",
        r"\1{REDACTED}",
        sanitized,
        flags=re.IGNORECASE,
    )
    return sanitized


def preview_response(response: requests.Response, secret: str, limit: int = 1000) -> str:
    content_type = response.headers.get("Content-Type", "")
    if "text" in content_type.lower() or "json" in content_type.lower():
        preview = response.text[:limit]
    else:
        preview = response.content[:limit].decode("utf-8", errors="replace")

    preview = preview.replace("\r", "\\r").replace("\n", "\\n")
    return redact_key(preview, secret)


def call_hsr_api(hsr_key: str, request_time: str) -> requests.Response:
    params = {
        "tm": request_time,
        "cmp": "HSR",
        "qcd": "MSK",
        "obs": "ECHO",
        "map": "HB",
        "disp": "A",
        "authKey": hsr_key,
    }

    print(f"endpoint: {HSR_ENDPOINT}")
    print(f"request time: {request_time}")
    print("request params without authKey:")
    print(
        {
            key: value
            for key, value in params.items()
            if key.lower() != "authkey"
        }
    )

    return requests.get(HSR_ENDPOINT, params=params, timeout=30)


def main() -> None:
    load_env()
    hsr_key = get_hsr_key()
    request_time = hsr_request_time()

    try:
        response = call_hsr_api(hsr_key, request_time)
    except requests.RequestException as exc:
        raise SystemExit(
            f"HSR API request failed during HTTP request: {type(exc).__name__}"
        ) from None

    print(f"HTTP status: {response.status_code}")
    print(f"content-type: {response.headers.get('Content-Type')}")
    print(f"response bytes: {len(response.content)}")
    print("response preview:")
    print(preview_response(response, hsr_key))

    if not response.ok:
        raise SystemExit(f"HSR API request failed with HTTP {response.status_code}.")


if __name__ == "__main__":
    main()
