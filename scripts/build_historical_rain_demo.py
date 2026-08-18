from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:
    from pyproj import Transformer
except ImportError:  # pragma: no cover
    Transformer = None

try:
    from shapely.geometry import shape
    from shapely.ops import transform as shapely_transform
    from shapely.ops import unary_union
except ImportError:  # pragma: no cover
    shape = None
    shapely_transform = None
    unary_union = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"
OUTPUT_DIR = PROJECT_ROOT / "data" / "processed" / "weather_demo"
FINAL_SCENARIO_PATH = PROJECT_ROOT / "data" / "processed" / "final_mvp" / "final_mvp_scenario.json"
FINAL_ROUTE_GEOJSON = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "final_mvp"
    / "end_to_end_validation"
    / "e2e_full_journey.geojson"
)

KMA_AUTH_ENV = "LADER_HSR"
AWS_STATION_ENDPOINT = "https://apihub.kma.go.kr/api/typ01/url/stn_inf.php"
AWS_HOURLY_ENDPOINT = "https://apihub.kma.go.kr/api/typ01/url/awsh.php"
ASOS_HOURLY_ENDPOINT = "https://apihub.kma.go.kr/api/typ01/url/kma_sfctm3.php"
REFERENCE_ASOS_DIR = OUTPUT_DIR / "reference_asos"
PRIMARY_AWS_STATION_ID = "412"
PRIMARY_AWS_EVENT_PREFIX = "2024-07-17"

FROZEN_ROUTE_REFERENCE = {
    "scenario_id": "move_on_frozen_mvp_eunpyeong_nokbeon_bulgwang_v1",
    "route_id": "eunpyeong_gu_office_nokbeon_bulgwang_bulgwang1_dong_center",
    "last_mile_baseline_flood_overlap_m": 9.985,
    "last_mile_alternative_flood_overlap_m": 0.0,
}

# Candidate windows are intentionally limited to known Seoul heavy-rain periods
# in the project flood-trace years. This avoids downloading multi-year minute data.
EVENT_WINDOWS = [
    ("2022-08-08 서울권 집중호우", "202208080000", "202208092300"),
    ("2022-08-09 서울권 집중호우", "202208090000", "202208102300"),
    ("2023-07-13 장마 집중호우", "202307130000", "202307142300"),
    ("2024-07-17 수도권 장마", "202407170000", "202407182300"),
    ("2025-07-17 수도권 호우", "202507170000", "202507182300"),
]


@dataclass(frozen=True)
class Station:
    station_id: str
    station_name: str
    lat: float
    lon: float
    source_type: str
    distance_from_final_route_m: float | None


class KmaApiError(RuntimeError):
    def __init__(self, stage: str, status_code: int | None, content_type: str | None, body_preview: str):
        super().__init__(f"{stage} failed")
        self.stage = stage
        self.status_code = status_code
        self.content_type = content_type
        self.body_preview = body_preview


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


def get_auth_key() -> str:
    auth_key = os.getenv(KMA_AUTH_ENV, "").strip()
    print(f"KMA API Hub key loaded: {bool(auth_key)}")
    print(f"KMA API Hub key length: {len(auth_key)}")
    if not auth_key:
        raise RuntimeError("LADER_HSR 환경변수가 없습니다.")
    return auth_key


def redact_secret(text: str, secret: str) -> str:
    sanitized = text.replace(secret, "{REDACTED}") if secret else text
    return re.sub(r"([?&]authKey=)[^&\s]+", r"\1{REDACTED}", sanitized, flags=re.I)


def request_text(
    endpoint: str,
    params: dict[str, Any],
    auth_key: str,
    stage: str,
    verbose: bool = True,
) -> str:
    try:
        response = requests.get(endpoint, params=params, timeout=40)
    except requests.RequestException as exc:
        raise KmaApiError(stage, None, None, type(exc).__name__) from None

    preview = redact_secret(response.text[:500].replace("\r", "\\r").replace("\n", "\\n"), auth_key)
    if verbose:
        print(f"[KMA] HTTP status: {response.status_code}")
        print(f"[KMA] content-type: {response.headers.get('Content-Type')}")
        print(f"[KMA] response preview: {preview}")

    if not response.ok:
        raise KmaApiError(stage, response.status_code, response.headers.get("Content-Type"), preview)
    return response.text


def parse_xml_items(text: str) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []

    items: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        row = {child.tag: (child.text or "").strip() for child in item}
        if row:
            items.append(row)
    return items


def parse_json_items(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})
    if isinstance(items, dict):
        item = items.get("item", [])
    else:
        item = items
    if isinstance(item, dict):
        return [item]
    return item if isinstance(item, list) else []


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-9", "-99", "-999", "-9999", "NA", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def final_route_geometry_projected():
    if shape is None or shapely_transform is None or unary_union is None or Transformer is None:
        return None
    geojson = json.loads(FINAL_ROUTE_GEOJSON.read_text(encoding="utf-8"))
    geoms = [shape(feature["geometry"]) for feature in geojson.get("features", []) if feature.get("geometry")]
    if not geoms:
        return None
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5186", always_xy=True)
    return shapely_transform(transformer.transform, unary_union(geoms))


def point_to_route_distance_m(lat: float, lon: float, route_geom: Any) -> float | None:
    if route_geom is None or Transformer is None or shapely_transform is None or shape is None:
        return None
    from shapely.geometry import Point

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5186", always_xy=True)
    point = shapely_transform(transformer.transform, Point(lon, lat))
    return round(float(route_geom.distance(point)), 3)


def fetch_aws_stations(auth_key: str) -> list[Station]:
    text = request_text(
        AWS_STATION_ENDPOINT,
        {
            "inf": "AWS",
            "stn": "",
            "tm": "",
            "help": 1,
            "authKey": auth_key,
        },
        auth_key,
        "AWS station metadata",
    )

    route_geom = final_route_geometry_projected()
    stations: list[Station] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        if len(tokens) < 9:
            continue
        station_id = tokens[0].strip()
        lon = to_float(tokens[1])
        lat = to_float(tokens[2])
        station_name = tokens[8].strip()
        if not station_id or not station_name or lat is None or lon is None:
            continue
        distance = point_to_route_distance_m(lat, lon, route_geom)
        stations.append(
            Station(
                station_id=station_id,
                station_name=station_name,
                lat=lat,
                lon=lon,
                source_type="KMA_AWS_STN_INF",
                distance_from_final_route_m=distance,
            )
        )

    return sorted(
        stations,
        key=lambda stn: stn.distance_from_final_route_m
        if stn.distance_from_final_route_m is not None
        else math.inf,
    )


def parse_kma_table(text: str) -> tuple[list[str], list[dict[str, str]]]:
    header: list[str] = []
    rows: list[dict[str, str]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            names = line.lstrip("#").strip().split()
            if {"TM", "STN"}.issubset(set(names)):
                header = names
            continue

        if not header:
            continue

        values = [part.strip() for part in line.split(",")] if "," in line else line.split()
        if len(values) < len(header):
            continue
        rows.append(dict(zip(header, values[: len(header)])))

    return header, rows


def fetch_hourly_rain_at(auth_key: str, station_id: str, yyyymmddhhmm: str) -> dict[str, Any] | None:
    text = request_text(
        AWS_HOURLY_ENDPOINT,
        {
            "var": "RN",
            "tm": yyyymmddhhmm,
            "stn": station_id,
            "disp": 0,
            "help": 1,
            "authKey": auth_key,
        },
        auth_key,
        "AWS hourly rainfall",
        verbose=False,
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        if len(tokens) < 7:
            continue
        return {
            "TM": tokens[0],
            "STN": tokens[1],
            "RN_DAY": tokens[4],
            "RN_HR1": tokens[6],
        }
    return None


def iter_hours(start: str, end: str) -> list[str]:
    current = datetime.strptime(start, "%Y%m%d%H%M")
    finish = datetime.strptime(end, "%Y%m%d%H%M")
    hours = []
    while current <= finish:
        hours.append(current.strftime("%Y%m%d%H%M"))
        current += timedelta(hours=1)
    return hours


def row_timestamp(row: dict[str, Any], fallback: str) -> str:
    raw = str(row.get("TM") or fallback)
    for fmt in ("%Y%m%d%H%M", "%Y%m%d%H"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return datetime.strptime(fallback, "%Y%m%d%H%M").strftime("%Y-%m-%d %H:%M:%S")


def row_hourly_rain(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    for field in ("RN_HR1", "RN_1HR", "RN", "VAL"):
        value = to_float(row.get(field))
        if value is not None:
            return value if value >= 0 else None
    return None


def fetch_asos_hourly_rain_period(
    auth_key: str,
    station: Station,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    text = request_text(
        ASOS_HOURLY_ENDPOINT,
        {
            "tm1": start,
            "tm2": end,
            "stn": station.station_id,
            "help": 1,
            "authKey": auth_key,
        },
        auth_key,
        "ASOS hourly rainfall period",
    )

    rows_by_time: dict[str, dict[str, Any]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        if len(tokens) < 16:
            continue
        timestamp = tokens[0]
        rainfall = to_float(tokens[15])
        rows_by_time[timestamp] = {"TM": timestamp, "RN": rainfall}

    series: list[dict[str, Any]] = []
    cumulative = 0.0
    for hour in iter_hours(start, end):
        compact_hour = hour[:10]
        row = rows_by_time.get(compact_hour) or rows_by_time.get(hour)
        rainfall = row_hourly_rain(row)
        missing = rainfall is None
        if rainfall is not None:
            cumulative += rainfall

        series.append(
            {
                "timestamp_kst": row_timestamp(row or {}, hour),
                "station_id": station.station_id,
                "station_name": station.station_name,
                "rainfall_mm_1h": rainfall,
                "cumulative_rainfall_mm": None if missing else round(cumulative, 3),
                "source": ASOS_HOURLY_ENDPOINT,
                "source_type": "KMA_API_HUB_ASOS_HOURLY",
                "missing_flag": missing,
            }
        )
    return series


def build_series(auth_key: str, station: Station, start: str, end: str) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    cumulative = 0.0
    for hour in iter_hours(start, end):
        row = fetch_hourly_rain_at(auth_key, station.station_id, hour)
        rainfall = row_hourly_rain(row)
        missing = rainfall is None
        if rainfall is not None:
            cumulative += rainfall

        series.append(
            {
                "timestamp_kst": row_timestamp(row or {}, hour),
                "station_id": station.station_id,
                "station_name": station.station_name,
                "rainfall_mm_1h": rainfall,
                "cumulative_rainfall_mm": None if missing else round(cumulative, 3),
                "source": AWS_HOURLY_ENDPOINT,
                "source_type": "KMA_AWS",
                "missing_flag": missing,
            }
        )
    return series


def summarize_event(name: str, station: Station, series: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in series if row["rainfall_mm_1h"] is not None]
    total = round(sum(row["rainfall_mm_1h"] for row in valid), 3)
    peak = max(valid, key=lambda row: row["rainfall_mm_1h"]) if valid else None
    return {
        "event_name": name,
        "station_id": station.station_id,
        "station_name": station.station_name,
        "daily_or_window_rainfall_mm": total,
        "max_hourly_rainfall_mm": peak["rainfall_mm_1h"] if peak else None,
        "rainfall_start": next((row["timestamp_kst"] for row in valid if row["rainfall_mm_1h"] > 0), None),
        "rainfall_peak": peak["timestamp_kst"] if peak else None,
        "rainfall_end": next((row["timestamp_kst"] for row in reversed(valid) if row["rainfall_mm_1h"] > 0), None),
        "valid_hourly_observations": len(valid),
        "missing_count": len(series) - len(valid),
        "flood_trace_year_match": name[:4] in {"2022", "2023", "2024", "2025"},
    }


def quality_report(series: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps = [row["timestamp_kst"] for row in series]
    duplicate_timestamps = sorted({ts for ts in timestamps if timestamps.count(ts) > 1})
    negative = [row for row in series if row["rainfall_mm_1h"] is not None and row["rainfall_mm_1h"] < 0]
    impossible = [row for row in series if row["rainfall_mm_1h"] is not None and row["rainfall_mm_1h"] > 200]
    missing = [row for row in series if row["missing_flag"]]
    return {
        "timestamp_timezone": "KST",
        "duplicate_timestamps": duplicate_timestamps,
        "missing_interval_count": len(missing),
        "negative_rainfall_count": len(negative),
        "impossible_hourly_value_count_over_200mm": len(impossible),
        "cumulative_rainfall_calculated_from_non_missing_values_only": True,
    }


def write_outputs(
    station_candidates: list[Station],
    selected_station: Station,
    event_candidates: list[dict[str, Any]],
    selected_event: dict[str, Any],
    timeline: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    is_asos = selected_station.source_type.startswith("KMA_ASOS")
    source_endpoint = ASOS_HOURLY_ENDPOINT if is_asos else AWS_HOURLY_ENDPOINT
    source_type = "KMA_API_HUB_ASOS_HOURLY" if is_asos else "KMA_AWS"

    event_payload = {
        "event_id": "kma_asos_historical_heavy_rain_demo_v1" if is_asos else "kma_aws_historical_heavy_rain_demo_v1",
        "event_date": selected_event["event_name"][:10],
        "station": selected_station.__dict__,
        "why_selected": report["why_selected"],
        "observation_period": {
            "start": timeline[0]["timestamp_kst"],
            "end": timeline[-1]["timestamp_kst"],
            "timezone": "KST",
        },
        "source": source_endpoint,
        "source_type": source_type,
        "frozen_route_reference": FROZEN_ROUTE_REFERENCE,
        "rainfall_station_id": selected_station.station_id,
        "rainfall_station_name": selected_station.station_name,
        "rainfall_source": source_type,
        "limitations": [
            "과거 강수 관측값은 경로 통행 가능/불가능 판정이 아니다.",
            "침수흔적은 과거 공간 노출 근거이며 침수확률로 해석하지 않는다.",
            "AWS 서대문(412)의 관측값은 최종 경로 지점에서 직접 측정한 강수량이 아니라, 최종 경로와 가장 가까운 현재 확보 가능 AWS 관측자료이다.",
            "이번 단계에서는 강수 threshold와 경로 상태 판단을 생성하지 않았다.",
        ],
    }

    (OUTPUT_DIR / "historical_heavy_rain_event.json").write_text(
        json.dumps(event_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "historical_heavy_rain_timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (OUTPUT_DIR / "historical_heavy_rain_report.json").write_text(
        json.dumps(
            {
                **report,
                "candidate_stations": [station.__dict__ for station in station_candidates],
                "event_candidates": event_candidates,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    csv_path = OUTPUT_DIR / "historical_heavy_rain_timeline.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(timeline[0].keys()))
        writer.writeheader()
        writer.writerows(timeline)


def preserve_existing_asos_reference() -> list[str]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    REFERENCE_ASOS_DIR.mkdir(parents=True, exist_ok=True)
    preserved: list[str] = []
    filenames = [
        "historical_heavy_rain_event.json",
        "historical_heavy_rain_timeline.csv",
        "historical_heavy_rain_timeline.json",
        "historical_heavy_rain_report.json",
    ]

    for filename in filenames:
        source = OUTPUT_DIR / filename
        if not source.exists():
            continue
        target = REFERENCE_ASOS_DIR / filename
        should_copy = True
        if source.suffix.lower() == ".json":
            try:
                payload = json.loads(source.read_text(encoding="utf-8"))
                source_type_text = json.dumps(payload, ensure_ascii=False)
                should_copy = "ASOS" in source_type_text or not target.exists()
            except json.JSONDecodeError:
                should_copy = not target.exists()
        if should_copy:
            shutil.copy2(source, target)
            preserved.append(str(target.relative_to(PROJECT_ROOT)))

    return preserved


def write_blocked_report(error: KmaApiError, auth_key_exists: bool) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / "historical_heavy_rain_report.json"
    report = {
        "status": "BLOCKED",
        "blocked_stage": error.stage,
        "auth_env_var": KMA_AUTH_ENV,
        "auth_key_exists": auth_key_exists,
        "http_status": error.status_code,
        "content_type": error.content_type,
        "response_body_preview": error.body_preview,
        "used_kma_data_type_attempted": [
            "KMA API Hub AWS station list",
            "KMA API Hub AWS hourly rainfall(RN_HR1)",
            "KMA API Hub ASOS hourly period data fallback",
        ],
        "required_action": (
            "KMA API Hub에서 지상관측 > 방재기상관측(AWS) 시간통계 자료 또는 "
            "종관기상관측(ASOS) 시간자료 활용신청이 필요합니다."
        ),
        "frozen_route_reference": FROZEN_ROUTE_REFERENCE,
        "not_created": [
            "historical_heavy_rain_event.json",
            "historical_heavy_rain_timeline.csv",
            "historical_heavy_rain_timeline.json",
            "강수 threshold",
            "NORMAL/REROUTE/STOP 경로 상태",
            "Risk Score",
            "Mobility Failure Point",
            "Last Accessible Departure",
        ],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def fallback_asos_station() -> Station:
    route_geom = final_route_geometry_projected()
    lat = 37.5714
    lon = 126.9658
    return Station(
        station_id="108",
        station_name="서울",
        lat=lat,
        lon=lon,
        source_type="KMA_ASOS_FALLBACK_PUBLIC_STATION_METADATA",
        distance_from_final_route_m=point_to_route_distance_m(lat, lon, route_geom),
    )


def probe_asos_fallback(auth_key: str) -> dict[str, Any]:
    try:
        text = request_text(
            ASOS_HOURLY_ENDPOINT,
            {
                "tm1": "202208080000",
                "tm2": "202208082300",
                "stn": "108",
                "help": 1,
                "authKey": auth_key,
            },
            auth_key,
            "ASOS hourly rainfall fallback",
        )
    except KmaApiError as error:
        return {
            "http_status": error.status_code,
            "content_type": error.content_type,
            "response_body_preview": error.body_preview,
        }
    return {"http_status": 200, "content_type": "text", "response_body_preview": text[:500]}


def main() -> None:
    load_env()
    auth_key = get_auth_key()
    preserved_asos_files = preserve_existing_asos_reference()

    aws_metadata_error: KmaApiError | None = None
    try:
        stations = fetch_aws_stations(auth_key)
    except KmaApiError as error:
        aws_metadata_error = error
        stations = [fallback_asos_station()]

    if not stations:
        raise RuntimeError("KMA AWS 관측소 메타데이터를 파싱하지 못했습니다.")

    candidate_stations = stations[:5]
    primary_station = next((station for station in stations if station.station_id == PRIMARY_AWS_STATION_ID), None)
    selected_station = primary_station or candidate_stations[0]
    if selected_station not in candidate_stations:
        candidate_stations = [selected_station, *candidate_stations[:4]]

    event_candidates: list[dict[str, Any]] = []
    event_series_by_name: dict[str, list[dict[str, Any]]] = {}
    for event_name, start, end in EVENT_WINDOWS:
        if selected_station.source_type.startswith("KMA_ASOS"):
            series = fetch_asos_hourly_rain_period(auth_key, selected_station, start, end)
        else:
            series = build_series(auth_key, selected_station, start, end)
        summary = summarize_event(event_name, selected_station, series)
        event_candidates.append(summary)
        event_series_by_name[event_name] = series

    event_candidates = sorted(
        event_candidates,
        key=lambda item: (
            -item["missing_count"],
            item["max_hourly_rainfall_mm"] or -1,
            item["daily_or_window_rainfall_mm"] or -1,
        ),
        reverse=True,
    )[:5]
    selected_event = next(
        (event for event in event_candidates if event["event_name"].startswith(PRIMARY_AWS_EVENT_PREFIX)),
        event_candidates[0],
    )
    selected_timeline = event_series_by_name[selected_event["event_name"]]
    quality = quality_report(selected_timeline)
    is_asos = selected_station.source_type.startswith("KMA_ASOS")

    report = {
        "status": "FROZEN" if not is_asos else "REFERENCE_FALLBACK",
        "rainfall_input_frozen": not is_asos,
        "used_kma_data_type": "KMA API Hub ASOS 지상관측 시간자료(RN)" if is_asos else "KMA API Hub AWS 시간통계 강수 자료(RN_HR1)",
        "endpoint": ASOS_HOURLY_ENDPOINT if is_asos else AWS_HOURLY_ENDPOINT,
        "station_metadata_endpoint": None if is_asos else AWS_STATION_ENDPOINT,
        "aws_metadata_status": {
            "available": aws_metadata_error is None,
            "http_status": aws_metadata_error.status_code if aws_metadata_error else 200,
            "message": aws_metadata_error.body_preview if aws_metadata_error else "OK",
        },
        "selected_station": selected_station.__dict__,
        "why_selected": [
            "최종 rainfall source를 KMA AWS 서대문(412)으로 확정했다." if not is_asos else "AWS 관측소 메타데이터 API가 차단되어 ASOS 서울(108) 시간자료를 fallback으로 사용했다.",
            "2024-07-17 수도권 장마 이벤트를 최종 demo event로 확정하고 API 응답값으로 재계산했다." if not is_asos else "검토 후보 중 결측이 가장 적으면서 시간당 강수 피크가 큰 이벤트를 우선 선정했다.",
            "KMA API Hub 응답에서 시간별 관측값이 파싱 가능했다.",
        ],
        "selected_event": selected_event,
        "timeline_start": selected_timeline[0]["timestamp_kst"],
        "timeline_end": selected_timeline[-1]["timestamp_kst"],
        "quality": quality,
        "asos_reference_preserved_files": preserved_asos_files,
        "rainfall_interpretation": "AWS 서대문(412)의 관측값은 최종 경로 위 직접 측정값이 아니라 최종 경로와 가장 가까운 현재 확보 가능 AWS 관측자료이다.",
        "frozen_route_reference": FROZEN_ROUTE_REFERENCE,
        "not_created": [
            "강수 threshold",
            "NORMAL/REROUTE/STOP 경로 상태",
            "Risk Score",
            "Mobility Failure Point",
            "Last Accessible Departure",
        ],
    }

    write_outputs(candidate_stations, selected_station, event_candidates, selected_event, selected_timeline, report)

    print("\n=== MOVE:ON 과거 폭우 demo dataset ===")
    print(f"1. 최종 rainfall source: {'KMA AWS' if not is_asos else 'KMA ASOS fallback'}")
    print(f"2. station: {selected_station.station_name}({selected_station.station_id})")
    print(f"3. final route distance: {selected_station.distance_from_final_route_m}m")
    print(f"4. 최종 event: {selected_event['event_name']}")
    print(f"5. timeline 시작/종료: {report['timeline_start']} ~ {report['timeline_end']}")
    print(
        f"6. peak rainfall/time: "
        f"{selected_event['max_hourly_rainfall_mm']}mm/h at {selected_event['rainfall_peak']}"
    )
    print(f"7. cumulative rainfall: {selected_event['daily_or_window_rainfall_mm']}mm")
    print(f"8. missing count: {selected_event['missing_count']}")
    print("9. ASOS reference 보존 위치:")
    if preserved_asos_files:
        for path in preserved_asos_files:
            print(f"   - {path}")
    else:
        print("   - 기존 ASOS reference 파일 없음 또는 이미 보존됨")
    print("10. 최종 AWS dataset 파일:")
    print("   - data/processed/weather_demo/historical_heavy_rain_event.json")
    print("   - data/processed/weather_demo/historical_heavy_rain_timeline.csv")
    print("   - data/processed/weather_demo/historical_heavy_rain_timeline.json")
    print("   - data/processed/weather_demo/historical_heavy_rain_report.json")
    print(f"11. quality validation 결과: {quality}")
    print("\n참고 관측소 후보:")
    for station in candidate_stations:
        selected = " (최종)" if station.station_id == selected_station.station_id else ""
        print(
            f"   - {station.station_name}({station.station_id}){selected}: "
            f"route distance={station.distance_from_final_route_m}m"
        )
    print("\n검토한 폭우 이벤트 후보 최대 5개:")
    for event in event_candidates:
        print(
            f"   - {event['event_name']}: peak={event['max_hourly_rainfall_mm']}mm/h "
            f"at {event['rainfall_peak']}, cumulative={event['daily_or_window_rainfall_mm']}mm, "
            f"missing={event['missing_count']}"
        )
    print("\nRainfall input frozen: TRUE")
    print("\n다음 단계:\n실제 AWS 강수 시간축과 정적 경로 취약성 데이터를 결합한\nMOVE:ON 규칙 기반 route-state 판단 설계")


if __name__ == "__main__":
    main()
