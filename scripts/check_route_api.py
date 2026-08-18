from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

import requests


API_URL = "https://apis.data.go.kr/B553766/path2/getShtrmPath2"
RAW_DIR = Path("data/raw/routes")
PROCESSED_DIR = Path("data/processed/routes")
ROUTE_API_KEY_ENV = "ROUTE_API_KEY"
TEST_SEARCH_DT = "2026-08-14 18:00:00"

PRIMARY_OD = ("\ubd88\uad11", "\uc5f0\uc2e0\ub0b4")  # Bulgwang -> Yeonsinnae
ROUTE_TESTS = (
    {
        "route_id": "Route A",
        "search_type": "duration",
        "through_stations": None,
    },
    {
        "route_id": "Route B",
        "search_type": "duration",
        "through_stations": "\ub3c5\ubc14\uc704",  # Dokbawi
    },
)
CANDIDATE_STATIONS = [
    "\ub179\ubc88",  # Nokbeon
    "\ubd88\uad11",  # Bulgwang
    "\ub3c5\ubc14\uc704",  # Dokbawi
    "\uc5f0\uc2e0\ub0b4",  # Yeonsinnae
    "\uc751\uc554",  # Eungam
]


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
    route_key = os.getenv(ROUTE_API_KEY_ENV, env.get(ROUTE_API_KEY_ENV, "")).strip()
    exists = bool(route_key)
    contains_percent_encoding = bool(re.search(r"%[0-9A-Fa-f]{2}", route_key))
    print(f"[route:key] selected_env_var: {ROUTE_API_KEY_ENV}")
    print(f"[route:key] value_exists: {exists}")
    print(f"[route:key] string_length: {len(route_key)}")
    print(f"[route:key] contains_percent: {contains_percent_encoding}")

    if not route_key:
        raise ValueError(f"{ROUTE_API_KEY_ENV} was not found in .env.")

    normalized_key = unquote(route_key) if contains_percent_encoding else route_key
    print(f"[route:key:normalized] string_length: {len(normalized_key)}")
    print(f"[route:key:normalized] contains_percent: {bool(re.search(r'%[0-9A-Fa-f]{2}', normalized_key))}")
    return normalized_key


def kst_search_datetime() -> str:
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Seoul"))
    except Exception:
        now = datetime.now(timezone.utc) + timedelta(hours=9)
    return now.strftime("%Y-%m-%d %H:%M:%S")


def masked_preview(text: str, secrets: tuple[str, ...], limit: int = 500) -> str:
    preview = text[:limit]
    for secret in secrets:
        if secret:
            preview = preview.replace(secret, "***API_KEY***")
    return preview.replace("\r", "\\r").replace("\n", "\\n")


def sanitize_url(url: str) -> str:
    return re.sub(
        r"([?&]serviceKey=)[^&]*",
        r"\1{REDACTED}",
        url,
        flags=re.IGNORECASE,
    )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_to_dict(element: ElementTree.Element) -> Any:
    children = list(element)
    text = (element.text or "").strip()
    if not children:
        return text

    result: dict[str, Any] = {}
    for child in children:
        key = local_name(child.tag)
        value = xml_to_dict(child)
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


def parse_response(response: requests.Response, label: str, secrets: tuple[str, ...]) -> dict:
    content_type = response.headers.get("Content-Type", "")
    response_text = response.text or ""
    stripped = response_text.lstrip()

    print(f"[{label}] HTTP status: {response.status_code}")
    print(f"[{label}] Content-Type: {content_type or '(none)'}")
    if response.request and response.request.url:
        print(f"[{label}] sanitized request URL: {sanitize_url(response.request.url)}")
    if response_text:
        print(f"[{label}] Body preview: {masked_preview(response_text, secrets)}")
    else:
        print(f"[{label}] Body preview: (empty response)")

    if not stripped:
        raise ApiResponseError(f"[{label}] Empty response body.")

    lower_type = content_type.lower()
    looks_json = "json" in lower_type or stripped.startswith(("{", "["))
    looks_xml = "xml" in lower_type or stripped.startswith("<?xml") or stripped.startswith("<")
    looks_html = "html" in lower_type or stripped.lower().startswith(("<!doctype html", "<html"))

    if looks_json:
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ApiResponseError(f"[{label}] JSON parsing failed.") from exc
        if not isinstance(parsed, dict):
            raise ApiResponseError(f"[{label}] JSON top-level value is not an object.")
        parsed["_response_format"] = "json"
        if not response.ok:
            raise ApiResponseError(f"[{label}] HTTP {response.status_code} JSON error response.")
        return parsed

    if looks_html:
        raise ApiResponseError(f"[{label}] HTML response received instead of API data.")

    if looks_xml:
        try:
            root = ElementTree.fromstring(response_text)
        except ElementTree.ParseError as exc:
            raise ApiResponseError(f"[{label}] XML parsing failed.") from exc
        code = find_xml_value(root, ("resultCode", "returnReasonCode", "CODE"))
        message = find_xml_value(root, ("resultMsg", "returnAuthMsg", "returnReasonMsg", "MESSAGE", "errMsg"))
        if not response.ok:
            raise ApiResponseError(f"[{label}] HTTP {response.status_code} XML error response.")
        return {
            "_response_format": "xml",
            "_xml_root": local_name(root.tag),
            "_xml_result_code": code,
            "_xml_result_msg": message,
            "_xml": {local_name(root.tag): xml_to_dict(root)},
        }

    raise ApiResponseError(f"[{label}] Unknown response format.")


def call_route_api(
    api_key: str,
    api_key_secrets: tuple[str, ...],
    route_id: str,
    origin: str,
    destination: str,
    search_type: str,
    search_dt: str,
    station_value_type: str = "name",
    through_stations: str | None = None,
) -> tuple[int, dict]:
    service_key = api_key
    print(f"[route:key:before_request] string_length: {len(service_key)}")
    print(f"[route:key:before_request] contains_percent: {bool(re.search(r'%[0-9A-Fa-f]{2}', service_key))}")

    params = {
        "serviceKey": service_key,
        "dataType": "JSON",
        "dptreStn": origin,
        "arvlStn": destination,
        "searchDt": search_dt,
        "searchType": search_type,
        "stationValueType": station_value_type,
        "schInclYn": "N",
    }
    if through_stations:
        params["thrghStns"] = through_stations
    response = requests.get(API_URL, params=params, timeout=30)
    payload = parse_response(response, f"{route_id}:{origin}->{destination}:{search_type}", api_key_secrets)
    if payload.get("_response_format") != "json":
        raise ApiResponseError(
            f"[route:{origin}->{destination}:{search_type}] Expected JSON but received "
            f"{payload.get('_response_format')}."
        )
    return response.status_code, payload


def response_header(payload: dict) -> dict:
    if payload.get("_response_format") == "xml":
        return {
            "resultCode": payload.get("_xml_result_code"),
            "resultMsg": payload.get("_xml_result_msg"),
        }
    if isinstance(payload.get("header"), dict):
        return payload.get("header", {})
    return payload.get("response", {}).get("header", {})


def response_body(payload: dict) -> dict:
    if isinstance(payload.get("body"), dict):
        return payload.get("body", {})
    return payload.get("response", {}).get("body", {})


def response_paths(payload: dict) -> list[dict]:
    body = response_body(payload)
    paths = body.get("paths", [])
    if isinstance(paths, dict):
        path = paths.get("path", paths)
        if isinstance(path, list):
            return path
        if isinstance(path, dict):
            return [path]
        return []
    if isinstance(paths, list):
        return paths
    return []


def path_list_value(path: dict, keys: tuple[str, ...]) -> Any:
    value = first_value(path, keys)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def nested_value(row: dict, path: tuple[str, ...]) -> Any:
    value: Any = row
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def collect_nested_records(value: Any) -> list[dict]:
    records: list[dict] = []
    if isinstance(value, dict):
        records.append(value)
        for child in value.values():
            records.extend(collect_nested_records(child))
    elif isinstance(value, list):
        for child in value:
            records.extend(collect_nested_records(child))
    return records


def unique_values_from_records(records: list[dict], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for record in records:
        for key in keys:
            value = record.get(key)
            if value not in (None, ""):
                text = str(value)
                if text not in values:
                    values.append(text)
    return values


def segment_like_records(records: list[dict]) -> list[dict]:
    selected = []
    markers = ("Dstc", "ReqHr", "trsit", "Trsit", "transfer", "Transfer")
    for record in records:
        keys = list(record.keys())
        if any(any(marker in key for marker in markers) for key in keys):
            selected.append(
                {
                    key: record[key]
                    for key in keys
                    if any(marker in key for marker in markers)
                }
            )
    return selected


def station_name_from_station_obj(station: Any) -> Any:
    if isinstance(station, dict):
        return station.get("stnNm")
    return None


def line_name_from_station_obj(station: Any) -> Any:
    if isinstance(station, dict):
        return station.get("lineNm")
    return None


def append_unique_value(values: list[Any], value: Any) -> None:
    if value in (None, ""):
        return
    if value not in values:
        values.append(value)


def normalize_processed_route(
    route_id: str,
    search_type: str,
    origin: str,
    destination: str,
    via_stations: list[str],
    payload: dict,
) -> dict:
    body = response_body(payload)
    paths = response_paths(payload)
    first_path = paths[0] if paths else {}
    last_path = paths[-1] if paths else {}

    station_sequence: list[Any] = []
    line_sequence: list[Any] = []
    segments: list[dict] = []

    append_unique_value(station_sequence, station_name_from_station_obj(first_path.get("dptreStn")))
    append_unique_value(line_sequence, line_name_from_station_obj(first_path.get("dptreStn")))

    for path in paths:
        dptre_stn = path.get("dptreStn") if isinstance(path, dict) else None
        arvl_stn = path.get("arvlStn") if isinstance(path, dict) else None
        dptre_station_name = station_name_from_station_obj(dptre_stn)
        arvl_station_name = station_name_from_station_obj(arvl_stn)
        line_name = line_name_from_station_obj(dptre_stn)

        append_unique_value(station_sequence, dptre_station_name)
        append_unique_value(station_sequence, arvl_station_name)
        append_unique_value(line_sequence, line_name)
        append_unique_value(line_sequence, line_name_from_station_obj(arvl_stn))

        segments.append(
            {
                "departure_station": dptre_station_name,
                "arrival_station": arvl_station_name,
                "line": line_name,
                "distance_raw": path.get("stnSctnDstc"),
                "transfer_yn": path.get("trsitYn"),
            }
        )

    return {
        "route_id": route_id,
        "search_type": search_type,
        "origin": station_name_from_station_obj(first_path.get("dptreStn")) or origin,
        "destination": station_name_from_station_obj(last_path.get("arvlStn")) or destination,
        "via_stations": via_stations,
        "total_distance": body.get("totalDstc"),
        "total_required_time_raw": body.get("totalReqHr") or body.get("totalreqHr"),
        "transfer_count": body.get("trsitNmtm"),
        "transfer_stations": body.get("trfstnNms"),
        "station_sequence": station_sequence,
        "line_sequence": line_sequence,
        "segments": segments,
    }


def summarize_route_payload(
    route_id: str,
    search_type: str,
    payload: dict | None,
    error: str | None = None,
) -> dict:
    body = response_body(payload or {})
    paths = response_paths(payload or {})
    first_path = paths[0] if paths else {}
    last_path = paths[-1] if paths else {}
    records = collect_nested_records(paths)
    summary = {
        "route_id": route_id,
        "searchType": search_type,
        "totalDstc": first_value(body, ("totalDstc",)),
        "totalReqHr": first_value(body, ("totalReqHr", "totalreqHr")),
        "trsitNmtm": first_value(body, ("trsitNmtm",)),
        "trfstnNms": first_value(body, ("trfstnNms",)),
        "paths_len": len(paths),
        "departure_station": nested_value(first_path, ("dptreStn", "stnNm")),
        "departure_line": nested_value(first_path, ("dptreStn", "lineNm")),
        "arrival_station": nested_value(last_path, ("arvlStn", "stnNm")),
        "arrival_line": nested_value(last_path, ("arvlStn", "lineNm")),
        "path_station_names": unique_values_from_records(records, ("stnNm", "stnNm_", "dptreStnNm", "arvlStnNm")),
        "path_line_names": unique_values_from_records(records, ("lineNm", "lineNm_", "dptreLineNm", "arvlLineNm")),
        "segment_fields": segment_like_records(records),
        "error": error,
    }
    return summary


def print_route_summary_block(summary: dict) -> None:
    print(f"{summary['route_id']}")
    print(f"searchType: {summary['searchType']}")
    print(f"totalDstc: {summary['totalDstc']}")
    print(f"totalReqHr: {summary['totalReqHr']}")
    print(f"trsitNmtm: {summary['trsitNmtm']}")
    print(f"trfstnNms: {summary['trfstnNms']}")
    print(f"len(paths): {summary['paths_len']}")
    if summary["paths_len"]:
        print(f"departure_station: {summary['departure_station']}")
        print(f"departure_line: {summary['departure_line']}")
        print(f"arrival_station: {summary['arrival_station']}")
        print(f"arrival_line: {summary['arrival_line']}")
        print(f"path_station_names: {summary['path_station_names']}")
        print(f"path_line_names: {summary['path_line_names']}")
        print(f"segment_fields: {json.dumps(summary['segment_fields'], ensure_ascii=False)}")
    if summary.get("error"):
        print(f"error: {summary['error']}")


def response_items(payload: dict) -> list[dict]:
    body = response_body(payload)
    items = body.get("items", {})
    item = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(item, dict):
        return [item]
    if isinstance(item, list):
        return item
    return []


def first_value(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def to_number(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(str(value).replace(",", ""))
    except ValueError:
        return None
    if numeric.is_integer():
        return int(numeric)
    return numeric


def append_unique(values: list[str], value: Any) -> None:
    if value in (None, ""):
        return
    value = str(value)
    if value not in values:
        values.append(value)


def normalize_route(origin: str, destination: str, search_type: str, payload: dict) -> dict:
    header = response_header(payload)
    items = response_items(payload)
    first = items[0] if items else {}

    travel_time = to_number(first_value(first, ("totalReqHr", "totalreqHr")))
    distance = to_number(first_value(first, ("totalDstc",)))
    transfer_count = to_number(first_value(first, ("trsitNmtm",)))

    lines: list[str] = []
    stations: list[str] = []
    segments: list[dict] = []
    transfers: list[dict] = []

    previous_line = None
    for index, row in enumerate(items):
        from_station = first_value(row, ("stnNm",))
        to_station = first_value(row, ("stnNm_",))
        line = first_value(row, ("lineNm", "dptreLineNm", "lineNm_"))
        to_line = first_value(row, ("lineNm_", "arvlLineNm"))
        transfer_flag = first_value(row, ("trsitYn",))

        append_unique(lines, line)
        append_unique(lines, to_line)
        append_unique(stations, from_station)
        append_unique(stations, to_station)

        segment = {
            "index": index,
            "from_station": from_station,
            "to_station": to_station,
            "line": line,
            "to_line": to_line,
            "section_distance": to_number(first_value(row, ("stnSctnDstc",))),
            "required_time": to_number(first_value(row, ("reqHr",))),
            "waiting_time": to_number(first_value(row, ("wtngHr",))),
            "transfer_flag": transfer_flag,
            "terminal_station": first_value(row, ("tmnlStnNm",)),
            "train_departure_time": first_value(row, ("trainDptreTm",)),
            "train_arrival_time": first_value(row, ("trainArvlTm",)),
            "raw": row,
        }
        segments.append(segment)

        is_transfer = str(transfer_flag).upper() == "Y"
        if previous_line and line and previous_line != line:
            is_transfer = True
        if is_transfer:
            transfers.append(
                {
                    "station": from_station,
                    "from_line": previous_line,
                    "to_line": line,
                    "transfer_flag": transfer_flag,
                }
            )
        if line:
            previous_line = str(line)

    if not stations and first:
        append_unique(stations, first_value(first, ("dptreStn", "dptreStnNm")))
        append_unique(stations, first_value(first, ("arvlStn", "arvlStnNm")))

    return {
        "route_id": search_type,
        "search_type": search_type,
        "origin": origin,
        "destination": destination,
        "result_code": header.get("resultCode"),
        "result_msg": header.get("resultMsg"),
        "travel_time": travel_time,
        "distance": distance,
        "transfer_count": transfer_count,
        "lines": lines,
        "stations": stations,
        "segments": segments,
        "transfers": transfers,
        "raw_item_count": len(items),
    }


def route_signature(route: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return tuple(route.get("lines") or []), tuple(route.get("stations") or [])


def mark_duplicate_routes(routes: list[dict]) -> list[dict]:
    seen: dict[tuple[tuple[str, ...], tuple[str, ...]], str] = {}
    for route in routes:
        signature = route_signature(route)
        route["duplicate_of"] = None
        route["is_duplicate_candidate"] = False
        if signature in seen:
            route["duplicate_of"] = seen[signature]
            route["is_duplicate_candidate"] = True
        else:
            seen[signature] = route["route_id"]
    return routes


def unique_route_count(routes: list[dict]) -> int:
    return len({route_signature(route) for route in routes if route.get("stations") or route.get("lines")})


def run_route_tests(
    api_key: str,
    api_key_secrets: tuple[str, ...],
    origin: str,
    destination: str,
    search_dt: str,
) -> tuple[dict, dict]:
    raw_routes: dict[str, Any] = {}
    normalized_routes: list[dict] = []

    for route_test in ROUTE_TESTS:
        route_id = route_test["route_id"]
        search_type = route_test["search_type"]
        try:
            status, payload = call_route_api(
                api_key,
                api_key_secrets,
                route_id,
                origin,
                destination,
                search_type,
                search_dt,
                through_stations=route_test["through_stations"],
            )
            raw_routes[route_id] = {
                "search_type": search_type,
                "through_stations": route_test["through_stations"],
                "http_status": status,
                "payload": payload,
                "error": None,
            }
            via_stations = [route_test["through_stations"]] if route_test["through_stations"] else []
            normalized_routes.append(
                normalize_processed_route(
                    route_id,
                    search_type,
                    origin,
                    destination,
                    via_stations,
                    payload,
                )
            )
        except Exception:
            raw_routes[route_id] = {
                "search_type": search_type,
                "through_stations": route_test["through_stations"],
                "http_status": None,
                "payload": None,
                "error": "request_failed",
            }

    processed = {
        "origin": origin,
        "destination": destination,
        "search_dt": search_dt,
        "station_value_type": "name",
        "routes": normalized_routes,
        "route_count": len(normalized_routes),
    }
    raw = {
        "origin": origin,
        "destination": destination,
        "search_dt": search_dt,
        "endpoint": API_URL,
        "note": "API key and full request URLs are intentionally not stored.",
        "routes": raw_routes,
    }
    return raw, processed


def print_route_summary(processed: dict) -> None:
    print(f"\nRoute summary: {processed['origin']} -> {processed['destination']}")
    for route in processed["routes"]:
        print(f"- searchType: {route['search_type']}")
        print(f"  total travel_time: {route['travel_time']}")
        print(f"  total distance: {route['distance']}")
        print(f"  transfer_count: {route['transfer_count']}")
        print(f"  lines: {', '.join(route['lines']) if route['lines'] else '(none)'}")
        print(f"  stations: {' -> '.join(route['stations']) if route['stations'] else '(none)'}")
        if route["transfers"]:
            print(f"  transfers: {json.dumps(route['transfers'], ensure_ascii=False)}")
        else:
            print("  transfers: (none)")
        if route["is_duplicate_candidate"]:
            print(f"  duplicate_of: {route['duplicate_of']}")
    print(f"Unique candidate routes: {processed['unique_route_count']}")


def candidate_od_pairs() -> list[tuple[str, str]]:
    pairs = []
    for origin in CANDIDATE_STATIONS:
        for destination in CANDIDATE_STATIONS:
            if origin != destination and (origin, destination) != PRIMARY_OD:
                pairs.append((origin, destination))
    return pairs


def write_outputs(raw_results: list[dict], processed_results: list[dict]) -> dict[str, str]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    raw_file = RAW_DIR / f"route_api_{timestamp}.json"
    processed_file = PROCESSED_DIR / "candidate_routes.json"
    route_a_file = PROCESSED_DIR / "route_a_bulgwang_yeonsinnae_direct.json"
    route_b_file = PROCESSED_DIR / "route_b_bulgwang_dokbawi_yeonsinnae.json"

    raw_file.write_text(json.dumps(raw_results, ensure_ascii=False, indent=2), encoding="utf-8")
    processed_file.write_text(json.dumps(processed_results, ensure_ascii=False, indent=2), encoding="utf-8")

    outputs = {
        "raw_file": str(raw_file),
        "processed_file": str(processed_file),
    }

    routes = []
    for processed in processed_results:
        routes.extend(processed.get("routes", []))

    for route in routes:
        if route.get("route_id") == "Route A":
            route_a_file.write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")
            outputs["route_a_file"] = str(route_a_file)
        elif route.get("route_id") == "Route B":
            route_b_file.write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")
            outputs["route_b_file"] = str(route_b_file)

    return outputs


def main() -> None:
    try:
        api_key = get_api_key()
        api_key_secrets = (api_key,)
        search_dt = TEST_SEARCH_DT

        raw_results: list[dict] = []
        processed_results: list[dict] = []

        primary_raw, primary_processed = run_route_tests(api_key, api_key_secrets, PRIMARY_OD[0], PRIMARY_OD[1], search_dt)
        raw_results.append(primary_raw)
        processed_results.append(primary_processed)
        outputs = write_outputs(raw_results, processed_results)
        if "route_a_file" in outputs:
            print(f"Saved Route A: {outputs['route_a_file']}")
        if "route_b_file" in outputs:
            print(f"Saved Route B: {outputs['route_b_file']}")
    except Exception:
        print("Route API check failed. See safe HTTP status, Content-Type, sanitized URL, and body preview above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
