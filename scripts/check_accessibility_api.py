import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

import requests


SEOUL_BASE_URL = "http://openapi.seoul.go.kr:8088"
DATA_GO_BASE_URL = "https://apis.data.go.kr/B553766/wksn"
RAW_DIR = Path("data/raw/accessibility")
PROCESSED_DIR = Path("data/processed/accessibility")
ACCESSIBILITY_API_KEY_ENV = "ACCESSIBILITY_API_KEY"

CANDIDATE_STATIONS = [
    "\ubd88\uad11",   # bulgwang
    "\ub3c5\ubc14\uc704",  # dokbawi
    "\uc5f0\uc2e0\ub0b4",  # yeonsinnae
    "\ub179\ubc88",   # nokbeon
    "\uc751\uc554",   # eungam
]

ENDPOINTS = {
    "elevator": {
        "path": "getWksnElvtr",
        "facility_type": "elevator",
        "facility_label": "\uc5d8\ub9ac\ubca0\uc774\ud130",
    },
    "wheelchair_lift": {
        "path": "getWksnWhcllift",
        "facility_type": "wheelchair_lift",
        "facility_label": "\ud720\uccb4\uc5b4\ub9ac\ud504\ud2b8",
    },
}


class ApiResponseError(RuntimeError):
    pass


def load_env(path: Path = Path(".env")) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env

    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def get_api_key() -> str:
    env = load_env()
    value = os.getenv(ACCESSIBILITY_API_KEY_ENV) or env.get(ACCESSIBILITY_API_KEY_ENV)
    print(f"[accessibility:key] selected_env_var: {ACCESSIBILITY_API_KEY_ENV}")
    print(f"[accessibility:key] value_exists: {bool(value)}")
    print(f"[accessibility:key] string_length: {len(value) if value else 0}")
    if not value:
        raise ValueError(f"{ACCESSIBILITY_API_KEY_ENV} was not found in .env.")
    return unquote(value)


def masked_preview(text: str, secrets: tuple[str, ...], limit: int = 500) -> str:
    preview = text[:limit]
    for secret in secrets:
        if secret:
            preview = preview.replace(secret, "***API_KEY***")
    return preview.replace("\r", "\\r").replace("\n", "\\n")


def print_response_diagnostics(
    response: requests.Response,
    request_label: str,
    response_text: str,
    secrets: tuple[str, ...],
) -> None:
    print(f"[{request_label}] HTTP status_code: {response.status_code}")
    print(f"[{request_label}] Content-Type: {response.headers.get('Content-Type', '(none)')}")
    if response_text:
        print(f"[{request_label}] Body preview: {masked_preview(response_text, secrets)}")
    else:
        print(f"[{request_label}] Body preview: (empty response)")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_element_to_dict(element: ElementTree.Element) -> Any:
    children = list(element)
    text = (element.text or "").strip()
    if not children:
        return text

    result: dict = {}
    for child in children:
        key = local_name(child.tag)
        value = xml_element_to_dict(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def find_xml_value(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    wanted = {name.lower() for name in names}
    for node in element.iter():
        if local_name(node.tag).lower() in wanted:
            value = (node.text or "").strip()
            if value:
                return value
    return ""


def parse_response(
    response: requests.Response,
    request_label: str,
    secrets: tuple[str, ...],
) -> dict:
    content_type = response.headers.get("Content-Type", "").lower()
    response_text = response.text or ""
    stripped = response_text.lstrip()

    print_response_diagnostics(response, request_label, response_text, secrets)
    response.raise_for_status()

    if not stripped:
        raise ApiResponseError(f"[{request_label}] Empty response body.")

    looks_json = "json" in content_type or stripped.startswith(("{", "["))
    looks_xml = (
        "xml" in content_type
        or stripped.startswith("<?xml")
        or stripped.startswith("<RESULT")
        or stripped.startswith("<OpenAPI_ServiceResponse")
    )
    looks_html = "html" in content_type or stripped.lower().startswith(("<!doctype html", "<html"))

    if looks_json:
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ApiResponseError(
                f"[{request_label}] JSON parsing failed. Check the Content-Type and body preview above."
            ) from exc
        if not isinstance(parsed, dict):
            raise ApiResponseError(
                f"[{request_label}] JSON was parsed but the top-level value is not an object."
            )
        parsed["_response_format"] = "json"
        return parsed

    if looks_xml:
        try:
            root = ElementTree.fromstring(response_text)
        except ElementTree.ParseError as exc:
            raise ApiResponseError(
                f"[{request_label}] XML parsing failed. Check the body preview above."
            ) from exc

        code = find_xml_value(root, ("CODE", "resultCode", "returnReasonCode"))
        message = find_xml_value(
            root,
            ("MESSAGE", "resultMsg", "returnAuthMsg", "returnReasonMsg", "errMsg"),
        )
        print(f"[{request_label}] XML resultCode/CODE: {code or '(not found)'}")
        print(f"[{request_label}] XML resultMsg/MESSAGE: {message or '(not found)'}")
        return {
            "_response_format": "xml",
            "_xml_root": local_name(root.tag),
            "_xml_result_code": code,
            "_xml_result_msg": message,
            "_xml": {local_name(root.tag): xml_element_to_dict(root)},
        }

    if looks_html:
        raise ApiResponseError(f"[{request_label}] HTML response received instead of API data.")

    raise ApiResponseError(
        f"[{request_label}] Unknown response format. Check the Content-Type and body preview above."
    )


def request_seoul_endpoint(
    api_key: str,
    endpoint: dict[str, str],
    start_index: int = 1,
    end_index: int = 5,
) -> tuple[int, dict]:
    url = f"{SEOUL_BASE_URL}/{api_key}/json/{endpoint['path']}/{start_index}/{end_index}/"
    print(
        f"[seoul:{endpoint['path']}] Request format: "
        f"{SEOUL_BASE_URL}/{{KEY}}/json/{endpoint['path']}/{start_index}/{end_index}/"
    )
    response = requests.get(url, timeout=20)
    payload = parse_response(
        response,
        f"seoul:{endpoint['path']}:{start_index}-{end_index}",
        (api_key,),
    )
    if payload.get("_response_format") != "json":
        raise ApiResponseError(
            f"[seoul:{endpoint['path']}] Expected JSON from Seoul Open Data API but received "
            f"{payload.get('_response_format')}."
        )
    return response.status_code, payload


def request_data_go_endpoint(
    api_key: str,
    endpoint: dict[str, str],
    page_no: int = 1,
    num_rows: int = 1000,
) -> tuple[int, dict]:
    params = {
        "serviceKey": api_key,
        "dataType": "JSON",
        "pageNo": page_no,
        "numOfRows": num_rows,
    }
    print(
        f"[data.go.kr:{endpoint['path']}] Request format: "
        f"{DATA_GO_BASE_URL}/{endpoint['path']}?"
        f"serviceKey={{KEY}}&dataType=JSON&pageNo={page_no}&numOfRows={num_rows}"
    )
    response = requests.get(f"{DATA_GO_BASE_URL}/{endpoint['path']}", params=params, timeout=20)
    payload = parse_response(
        response,
        f"data.go.kr:{endpoint['path']}:page-{page_no}",
        (api_key,),
    )
    if payload.get("_response_format") != "json":
        raise ApiResponseError(
            f"[data.go.kr:{endpoint['path']}] Expected JSON from Public Data Portal API but received "
            f"{payload.get('_response_format')}."
        )
    return response.status_code, payload


def seoul_service_payload(payload: dict, service_name: str) -> dict:
    return payload.get(service_name, {})


def seoul_result(service_payload: dict) -> dict:
    return service_payload.get("RESULT", {})


def seoul_items(payload: dict, service_name: str) -> list[dict]:
    rows = seoul_service_payload(payload, service_name).get("row", [])
    if isinstance(rows, dict):
        return [rows]
    if isinstance(rows, list):
        return rows
    return []


def seoul_total_count(payload: dict, service_name: str) -> int:
    value = seoul_service_payload(payload, service_name).get("list_total_count") or 0
    return int(value)


def body_from_response(payload: dict) -> dict:
    return payload.get("response", {}).get("body", {})


def header_from_response(payload: dict) -> dict:
    return payload.get("response", {}).get("header", {})


def items_from_response(payload: dict) -> list[dict]:
    items = body_from_response(payload).get("items", {})
    item = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(item, dict):
        return [item]
    if isinstance(item, list):
        return item
    return []


def fetch_seoul_all(api_key: str) -> tuple[dict, list[dict]]:
    raw: dict = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "base_url": SEOUL_BASE_URL,
            "service": "Seoul Open Data Plaza - Seoul Metro transportation-vulnerable facility API",
            "note": "API key and full request URLs are intentionally not stored.",
        },
        "endpoints": {},
    }
    flattened: list[dict] = []

    for name, endpoint in ENDPOINTS.items():
        status, first_payload = request_seoul_endpoint(api_key, endpoint)
        total_count = seoul_total_count(first_payload, endpoint["path"])
        result = seoul_result(seoul_service_payload(first_payload, endpoint["path"]))
        rows = []

        if total_count:
            for start in range(1, total_count + 1, 1000):
                end = min(start + 999, total_count)
                status, payload = request_seoul_endpoint(api_key, endpoint, start, end)
                rows.extend(seoul_items(payload, endpoint["path"]))
        else:
            payload = first_payload
            rows = seoul_items(payload, endpoint["path"])

        raw["endpoints"][name] = {
            "endpoint_path": endpoint["path"],
            "http_status": status,
            "result": result,
            "body_meta": {
                "list_total_count": total_count,
                "row_count": len(rows),
            },
            "response": first_payload,
            "rows": rows,
        }

        for row in rows:
            enriched = dict(row)
            enriched["_facility_type"] = endpoint["facility_type"]
            enriched["_facility_label"] = endpoint["facility_label"]
            flattened.append(enriched)

    return raw, flattened


def fetch_data_go_all(api_key: str) -> tuple[dict, list[dict]]:
    raw: dict = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "base_url": DATA_GO_BASE_URL,
            "service": "Public Data Portal - Seoul Metro transportation-vulnerable facility API",
            "note": "API key and full request URLs are intentionally not stored.",
        },
        "endpoints": {},
    }
    flattened: list[dict] = []

    for name, endpoint in ENDPOINTS.items():
        status, payload = request_data_go_endpoint(api_key, endpoint)
        body = body_from_response(payload)
        total_count = int(body.get("totalCount") or 0)
        rows = items_from_response(payload)

        if total_count > len(rows):
            status, payload = request_data_go_endpoint(api_key, endpoint, page_no=1, num_rows=total_count)
            body = body_from_response(payload)
            rows = items_from_response(payload)

        raw["endpoints"][name] = {
            "endpoint_path": endpoint["path"],
            "http_status": status,
            "header": header_from_response(payload),
            "body_meta": {
                "pageNo": body.get("pageNo"),
                "numOfRows": body.get("numOfRows"),
                "totalCount": body.get("totalCount"),
            },
            "response": payload,
        }

        for row in rows:
            enriched = dict(row)
            enriched["_facility_type"] = endpoint["facility_type"]
            enriched["_facility_label"] = endpoint["facility_label"]
            flattened.append(enriched)

    return raw, flattened


def fetch_all(api_key: str) -> tuple[dict, list[dict]]:
    try:
        return fetch_data_go_all(api_key)
    except (ApiResponseError, requests.RequestException) as data_go_error:
        print(f"[data.go.kr] Primary Public Data Portal request failed: {data_go_error}")
        print("[seoul] Trying Seoul Open Data URL format as a fallback.")
        try:
            return fetch_seoul_all(api_key)
        except (ApiResponseError, requests.RequestException) as seoul_error:
            raise RuntimeError(
                "Both Seoul Open Data and Public Data Portal API formats failed."
            ) from seoul_error


def first_value(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def normalize_row(row: dict) -> dict:
    return {
        "station_name": first_value(row, ("stnNm", "STN_NM", "stationNm")),
        "station_code": first_value(row, ("stnCd", "STN_CD", "stationCd")),
        "line_name": first_value(row, ("lineNm", "LINE_NM", "line")),
        "facility_type": row.get("_facility_type", ""),
        "facility_label": row.get("_facility_label", ""),
        "nearby_entrance_no": first_value(row, ("vcntEntrcNo", "VCNT_ENTRC_NO", "entrcNo", "exitNo")),
        "location": first_value(row, ("dtlPstn", "DTL_PSTN", "pstn", "location")),
        "operation_status": first_value(row, ("oprtngSitu", "OPRTNG_SITU", "operationStatus", "status")),
        "raw": row,
    }


def build_processed(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    normalized = [normalize_row(row) for row in rows]
    candidate_rows = [
        row for row in normalized
        if row["station_name"] in CANDIDATE_STATIONS
    ]

    station_summary = []
    for station in CANDIDATE_STATIONS:
        station_rows = [row for row in candidate_rows if row["station_name"] == station]
        station_summary.append(
            {
                "station_name": station,
                "has_data": bool(station_rows),
                "elevator_count": sum(row["facility_type"] == "elevator" for row in station_rows),
                "wheelchair_lift_count": sum(row["facility_type"] == "wheelchair_lift" for row in station_rows),
                "operation_status_note": "If operation_status is blank, treat it as unverified.",
            }
        )
    return candidate_rows, station_summary


def collect_fields(rows: list[dict]) -> dict[str, list[str]]:
    fields: dict[str, set[str]] = {}
    for row in rows:
        facility_type = row.get("_facility_type", "unknown")
        fields.setdefault(facility_type, set()).update(row.keys())
    return {key: sorted(value) for key, value in fields.items()}


def write_outputs(raw: dict, rows: list[dict]) -> dict[str, str]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_file = RAW_DIR / f"accessibility_{timestamp}.json"
    processed_json = PROCESSED_DIR / "candidate_station_accessibility.json"
    processed_csv = PROCESSED_DIR / "candidate_station_accessibility.csv"
    summary_json = PROCESSED_DIR / "candidate_station_accessibility_summary.json"
    fields_json = PROCESSED_DIR / "accessibility_response_fields.json"

    candidate_rows, station_summary = build_processed(rows)

    raw_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    processed_json.write_text(json.dumps(candidate_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_json.write_text(json.dumps(station_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    fields_json.write_text(json.dumps(collect_fields(rows), ensure_ascii=False, indent=2), encoding="utf-8")

    with processed_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "station_name",
                "station_code",
                "line_name",
                "facility_type",
                "facility_label",
                "nearby_entrance_no",
                "location",
                "operation_status",
            ],
        )
        writer.writeheader()
        for row in candidate_rows:
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames})

    return {
        "raw_file": str(raw_file),
        "processed_json": str(processed_json),
        "processed_csv": str(processed_csv),
        "summary_json": str(summary_json),
        "fields_json": str(fields_json),
    }


def main() -> None:
    try:
        api_key = get_api_key()
        raw, rows = fetch_all(api_key)
        outputs = write_outputs(raw, rows)

        print("Accessibility API check completed.")
        for name, endpoint_result in raw["endpoints"].items():
            result = endpoint_result.get("result") or endpoint_result.get("header", {})
            code = result.get("CODE") or result.get("resultCode")
            message = result.get("MESSAGE") or result.get("resultMsg")
            count = (
                endpoint_result.get("body_meta", {}).get("list_total_count")
                or endpoint_result.get("body_meta", {}).get("totalCount")
            )
            print(
                f"- {name}: HTTP {endpoint_result.get('http_status')}, "
                f"resultCode={code}, resultMsg={message}, totalCount={count}"
            )
        print("Saved files:")
        for path in outputs.values():
            print(f"- {path}")
    except Exception as exc:
        print(f"Accessibility API check failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
