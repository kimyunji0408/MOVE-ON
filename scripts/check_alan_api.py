from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

ALAN_API_KEY_ENV = "ALAN_API_KEY"

ESTSOFT_ALAN_ENDPOINT: str | None = None
ESTSOFT_ALAN_METHOD: str | None = None
ESTSOFT_ALAN_AUTH_LOCATION: str | None = None
ESTSOFT_ALAN_AUTH_SCHEME: str | None = None
ESTSOFT_ALAN_CONTENT_TYPE: str | None = None
ESTSOFT_ALAN_PAYLOAD_KEYS: list[str] = []
ESTSOFT_ALAN_QUERY_PARAMETER_NAMES: list[str] = []
ENDPOINT_SOURCE = "not_found"


class AlanApiCheckError(RuntimeError):
    pass


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")

    return env


def get_api_key() -> str:
    env = load_env_file(ENV_FILE)
    key = os.getenv(ALAN_API_KEY_ENV, env.get(ALAN_API_KEY_ENV, "")).strip()

    print(f"[alan:key] selected_env_var: {ALAN_API_KEY_ENV}")
    print(f"[alan:key] value_exists: {bool(key)}")
    print(f"[alan:key] string_length: {len(key)}")
    print(f"[alan:key] contains_space: {' ' in key}")
    print(f"[alan:key] contains_newline: {chr(10) in key or chr(13) in key}")
    print(f"[alan:key] contains_dot: {'.' in key}")
    print(f"[alan:key] dot_segment_count: {len(key.split('.')) if key else 0}")
    print(
        "[alan:key] starts_with_bearer_literal: "
        f"{key.lower().startswith('bearer ')}"
    )

    if not key:
        raise AlanApiCheckError(f"{ALAN_API_KEY_ENV} was not found.")

    return key


def audit_current_request_shape(key: str) -> None:
    print("[alan:request] endpoint:", ESTSOFT_ALAN_ENDPOINT)
    print("[alan:request] method:", ESTSOFT_ALAN_METHOD)
    print("[alan:request] auth_location:", ESTSOFT_ALAN_AUTH_LOCATION)
    print("[alan:request] auth_scheme:", ESTSOFT_ALAN_AUTH_SCHEME)
    print("[alan:request] content_type:", ESTSOFT_ALAN_CONTENT_TYPE)
    print("[alan:request] payload_keys:", ESTSOFT_ALAN_PAYLOAD_KEYS)
    print("[alan:request] query_parameter_names:", ESTSOFT_ALAN_QUERY_PARAMETER_NAMES)
    print("[alan:request] key_length:", len(key))
    print("[alan:request] endpoint_source:", ENDPOINT_SOURCE)


def body_preview(text: str, limit: int = 1000) -> str:
    preview = text[:limit]
    return preview.replace("\r", "\\r").replace("\n", "\\n")


def print_response_structure(response: requests.Response) -> None:
    content_type = response.headers.get("Content-Type", "")
    text = response.text

    print(f"[alan] HTTP status: {response.status_code}")
    print(f"[alan] Content-Type: {content_type}")
    print(f"[alan] Body preview: {body_preview(text)}")

    if response.status_code == 401:
        print("[alan] Authentication failed (HTTP 401).")
        return

    stripped = text.strip()
    if not stripped:
        print("[alan] Empty response body.")
        return

    if "application/json" in content_type.lower() or stripped.startswith("{"):
        try:
            payload = response.json()
        except json.JSONDecodeError:
            print("[alan] JSON parsing failed.")
            return

        if isinstance(payload, dict):
            print(f"[alan] JSON top-level keys: {list(payload.keys())}")
        else:
            print(f"[alan] JSON top-level type: {type(payload).__name__}")
        return

    if "<html" in stripped[:200].lower():
        print("[alan] HTML response received.")
        return

    print("[alan] Non-JSON response format.")


def call_estsoft_alan(api_key: str) -> requests.Response:
    if (
        ESTSOFT_ALAN_ENDPOINT is None
        or ESTSOFT_ALAN_METHOD is None
        or ESTSOFT_ALAN_AUTH_LOCATION is None
        or ESTSOFT_ALAN_AUTH_SCHEME is None
        or ESTSOFT_ALAN_CONTENT_TYPE is None
    ):
        raise AlanApiCheckError(
            "Official ESTsoft Alan API endpoint/auth/request schema is not configured. "
            "No HTTP request was sent."
        )

    raise AlanApiCheckError(
        "Official ESTsoft Alan request implementation is not configured. "
        "No HTTP request was sent."
    )


def main() -> None:
    try:
        key = get_api_key()
        audit_current_request_shape(key)

        response = call_estsoft_alan(key)
        print_response_structure(response)

        if not response.ok:
            raise AlanApiCheckError(
                f"Alan API request failed with HTTP {response.status_code}."
            )
    except requests.RequestException as exc:
        raise SystemExit(
            f"Alan API check failed due to network/request error: {type(exc).__name__}"
        ) from None
    except AlanApiCheckError as exc:
        raise SystemExit(f"Alan API check stopped: {exc}") from None


if __name__ == "__main__":
    main()
