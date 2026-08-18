from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT_FOR_IMPORT))

from scripts.build_historical_rain_demo import (
    AWS_HOURLY_ENDPOINT,
    EVENT_WINDOWS,
    KmaApiError,
    build_series,
    fetch_aws_stations,
    get_auth_key,
    load_env,
    quality_report,
    request_text,
    summarize_event,
)


ASOS_REFERENCE = {
    "station_id": "108",
    "station_name": "서울",
    "route_distance_m": 4212.389,
    "selected_event": "2023-07-13",
    "peak_hourly_rainfall_mm": 34.7,
    "cumulative_rainfall_mm": 164.2,
    "missing_count": 1,
}


def print_asos_reference() -> None:
    print("\n=== Existing ASOS reference ===")
    print(f"station: {ASOS_REFERENCE['station_name']}({ASOS_REFERENCE['station_id']})")
    print(f"route distance: {ASOS_REFERENCE['route_distance_m']}m")
    print(f"selected event: {ASOS_REFERENCE['selected_event']}")
    print(f"peak hourly rainfall: {ASOS_REFERENCE['peak_hourly_rainfall_mm']}mm/h")
    print(f"cumulative rainfall: {ASOS_REFERENCE['cumulative_rainfall_mm']}mm")
    print(f"missing: {ASOS_REFERENCE['missing_count']}")


def rank_events(auth_key: str, station) -> tuple[list[dict], dict | None, dict | None]:
    event_candidates: list[dict] = []
    selected_quality = None
    selected_event = None

    for event_name, start, end in EVENT_WINDOWS:
        series = build_series(auth_key, station, start, end)
        summary = summarize_event(event_name, station, series)
        event_candidates.append(summary)

    event_candidates = sorted(
        event_candidates,
        key=lambda item: (
            -item["missing_count"],
            item["max_hourly_rainfall_mm"] or -1,
            item["daily_or_window_rainfall_mm"] or -1,
        ),
        reverse=True,
    )[:5]

    if event_candidates:
        selected_event = event_candidates[0]
        for event_name, start, end in EVENT_WINDOWS:
            if event_name == selected_event["event_name"]:
                selected_quality = quality_report(build_series(auth_key, station, start, end))
                break

    return event_candidates, selected_event, selected_quality


def probe_aws_rainfall(auth_key: str, station) -> tuple[bool, int | None, str | None]:
    try:
        request_text(
            AWS_HOURLY_ENDPOINT,
            {
                "var": "RN",
                "tm": "202307131500",
                "stn": station.station_id,
                "disp": 0,
                "help": 1,
                "authKey": auth_key,
            },
            auth_key,
            "AWS hourly rainfall probe",
        )
    except KmaApiError as error:
        return False, error.status_code, error.content_type
    return True, 200, "text/plain"


def main() -> None:
    load_env()
    auth_key = get_auth_key()

    print("\n=== AWS authorization check ===")
    try:
        stations = fetch_aws_stations(auth_key)
    except KmaApiError as error:
        print(f"AWS station metadata (stn_inf): {error.status_code}")
        print(f"AWS metadata content-type: {error.content_type}")
        print(f"AWS metadata response: {error.body_preview}")
        print("AWS rainfall (awsh RN): not tested")
        print_asos_reference()
        print("\nRecommendation: C. AWS API가 여전히 403 또는 자료 조회 실패이므로 ASOS 유지, AWS 권한 문제 보고")
        print("No ASOS files were overwritten.")
        return

    print("AWS station metadata (stn_inf): 200")
    if not stations:
        print("AWS station metadata parsed: 0 stations")
        print_asos_reference()
        print("\nRecommendation: C. AWS 관측소 메타데이터 파싱 실패로 ASOS 유지")
        print("No ASOS files were overwritten.")
        return

    print(f"AWS station metadata parsed: {len(stations)} stations")
    print("\n=== Closest AWS station candidates ===")
    for station in stations[:5]:
        print(
            f"- {station.station_name}({station.station_id}) "
            f"lat={station.lat}, lon={station.lon}, route distance={station.distance_from_final_route_m}m"
        )

    selected_station = stations[0]
    print("\n=== AWS rainfall availability ===")
    print(
        f"selected AWS station: {selected_station.station_name}({selected_station.station_id}), "
        f"route distance={selected_station.distance_from_final_route_m}m"
    )
    rainfall_ok, rainfall_status, rainfall_content_type = probe_aws_rainfall(auth_key, selected_station)
    print(f"AWS rainfall (awsh RN): {rainfall_status}")
    print(f"AWS rainfall content-type: {rainfall_content_type}")
    if not rainfall_ok:
        print_asos_reference()
        print("\nRecommendation: C. AWS 강수자료 조회 실패로 ASOS 유지, AWS 권한 문제 보고")
        print("No ASOS files were overwritten.")
        return

    try:
        event_candidates, selected_event, selected_quality = rank_events(auth_key, selected_station)
    except KmaApiError as error:
        print(f"AWS rainfall HTTP status: {error.status_code}")
        print(f"AWS rainfall content-type: {error.content_type}")
        print(f"AWS rainfall response: {error.body_preview}")
        print_asos_reference()
        print("\nRecommendation: C. AWS 강수자료 조회 실패로 ASOS 유지, AWS 권한 문제 보고")
        print("No ASOS files were overwritten.")
        return

    print("data availability period checked: 2022~2025 candidate heavy-rain windows")
    print("2022~2025 rainfall query success: True")
    print("\n=== AWS heavy-rain event candidates TOP 5 ===")
    for event in event_candidates:
        print(
            f"- {event['event_name']}: peak={event['max_hourly_rainfall_mm']}mm/h "
            f"at {event['rainfall_peak']}, cumulative={event['daily_or_window_rainfall_mm']}mm, "
            f"missing={event['missing_count']}"
        )

    print_asos_reference()

    if selected_event is None:
        recommendation = "C. AWS 이벤트 후보 산출 실패로 ASOS 유지"
    elif selected_station.distance_from_final_route_m is None:
        recommendation = "B. AWS 자료는 조회됐지만 관측소 거리 검증이 불완전하므로 ASOS 유지 추천"
    elif selected_station.distance_from_final_route_m < ASOS_REFERENCE["route_distance_m"] and selected_event["missing_count"] <= 3:
        recommendation = "A. AWS 관측소가 훨씬 가깝고 데이터 품질도 충분하므로 AWS를 최종 demo rainfall source로 추천"
    elif selected_station.distance_from_final_route_m < ASOS_REFERENCE["route_distance_m"]:
        recommendation = "B. AWS는 가깝지만 결측/자료 품질이 좋지 않아 ASOS 유지 추천"
    else:
        recommendation = "B. AWS가 ASOS보다 명확히 가깝지 않아 기존 ASOS 유지 추천"

    print("\n=== AWS vs ASOS recommendation ===")
    print(f"AWS selected event: {selected_event}")
    print(f"AWS selected quality: {selected_quality}")
    print(f"Recommendation: {recommendation}")
    print("No ASOS files were overwritten.")


if __name__ == "__main__":
    main()
