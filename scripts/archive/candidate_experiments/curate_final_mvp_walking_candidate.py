from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import pandas as pd

from build_osm_walking_routing import GRAPHML_PATH, METRIC_CRS, PROJECT_ROOT, rel, route_linestring, write_json


SOURCE_DIR = PROJECT_ROOT / "data/processed/osm_routing_targeted"
OUTPUT_DIR = PROJECT_ROOT / "data/processed/final_mvp"

BASELINES_CSV = SOURCE_DIR / "all_baseline_routes.csv"
K_ALTERNATIVES_CSV = SOURCE_DIR / "targeted_route_alternatives.csv"
AVOIDANCE_CSV = SOURCE_DIR / "historical_flood_trace_avoidance_routes.csv"

CURATED_TOP5_JSON = OUTPUT_DIR / "curated_top5_walking_candidates.json"
CURATED_TOP5_GEOJSON = OUTPUT_DIR / "curated_top5_walking_candidates.geojson"
FINAL_SCENARIO_JSON = OUTPUT_DIR / "final_mvp_scenario.json"
REPORT_JSON = OUTPUT_DIR / "final_mvp_candidate_selection_report.json"

INFRA_KEYWORDS = [
    "빗물받이",
    "맨홀",
    "과속방지턱",
    "의자",
    "볼라드",
    "진입방지봉",
    "보안등",
    "가로등",
    "CCTV",
]

LOW_PRIORITY_KEYWORDS = [
    "공인중개사",
    "부동산",
    "세탁",
    "냉삼",
    "면옥",
    "호우동",
    "색연필",
    "크리닝",
]

HIGH_PRIORITY_KEYWORDS = [
    "공동생활가정",
    "예수의집",
    "사랑의집",
    "장애인화장실",
    "복지",
    "보건소",
    "주민센터",
    "장애인",
]

MIN_VISIBLE_DISTANCE_DELTA_M = 15.0


def read_routes() -> pd.DataFrame:
    frames = []
    for path in [K_ALTERNATIVES_CSV, AVOIDANCE_CSV]:
        df = pd.read_csv(path, encoding="utf-8-sig")
        if not df.empty:
            frames.append(df)
    routes = pd.concat(frames, ignore_index=True)
    routes = routes[routes["historical_flood_trace_avoiding_candidate"].astype(str).str.lower().eq("true")].copy()
    return routes


def has_keyword(text: str, keywords: list[str]) -> bool:
    return any(keyword.lower() in text.lower() for keyword in keywords)


def classify_facility(row: pd.Series) -> tuple[str, int]:
    text = f"{row.get('facility_name', '')} {row.get('facility_address', '')}"
    if has_keyword(text, INFRA_KEYWORDS):
        return "infrastructure_object", 0
    if has_keyword(text, HIGH_PRIORITY_KEYWORDS):
        return "clear_user_destination", 3
    if has_keyword(text, LOW_PRIORITY_KEYWORDS):
        return "low_priority_private_business", 1
    if row.get("facility_source") == "mobility_facility":
        return "named_mobility_facility", 2
    return "other_named_destination", 1


def parse_nodes(value: object) -> list[int]:
    return [int(node) for node in json.loads(str(value))]


def geometry_metrics(graph, baseline_nodes: list[int], alternative_nodes: list[int]) -> dict:
    baseline = gpd.GeoSeries([route_linestring(graph, baseline_nodes)], crs="EPSG:4326").to_crs(METRIC_CRS).iloc[0]
    alternative = gpd.GeoSeries([route_linestring(graph, alternative_nodes)], crs="EPSG:4326").to_crs(METRIC_CRS).iloc[0]
    inter_len = baseline.buffer(1).intersection(alternative.buffer(1)).length
    overlap_proxy = inter_len / max(baseline.length + alternative.length, 1.0)
    return {
        "hausdorff_distance_m": round(float(baseline.hausdorff_distance(alternative)), 3),
        "baseline_geometry_length_m": round(float(baseline.length), 3),
        "alternative_geometry_length_m": round(float(alternative.length), 3),
        "geometry_overlap_proxy": round(float(overlap_proxy), 6),
    }


def curate() -> tuple[pd.DataFrame, list[dict], dict]:
    baselines = pd.read_csv(BASELINES_CSV, encoding="utf-8-sig")
    baseline_by_od = baselines.set_index("od_id")
    routes = read_routes()
    graph = ox.load_graphml(GRAPHML_PATH)

    rows = []
    for _, row in routes.iterrows():
        facility_class, user_score = classify_facility(row)
        baseline = baseline_by_od.loc[row["od_id"]]
        metrics = geometry_metrics(graph, parse_nodes(baseline["node_sequence"]), parse_nodes(row["node_sequence"]))
        visible_score = 1 if float(row["distance_delta_m"]) >= MIN_VISIBLE_DISTANCE_DELTA_M else 0
        if metrics["hausdorff_distance_m"] >= 20:
            visible_score += 1
        if float(row["alternative_flood_overlap_length_m"]) <= 0.01:
            overlap_zero_score = 1
        else:
            overlap_zero_score = 0
        enriched = row.to_dict()
        enriched.update(metrics)
        enriched.update(
            {
                "facility_class": facility_class,
                "user_destination_score": user_score,
                "visual_demo_score": visible_score,
                "avoidance_overlap_zero_score": overlap_zero_score,
                "usable_as_first_mile": True,
                "usable_as_last_mile": True,
                "end_to_end_candidate_possible": True,
                "selection_note": (
                    "실제 사용자가 갈 법한 목적시설, 침수흔적 중첩 감소, 과도하지 않은 추가거리, "
                    "지도상 경로 차이를 기준으로 재선정"
                ),
            }
        )
        rows.append(enriched)

    df = pd.DataFrame(rows)
    df = df[~df["facility_class"].eq("infrastructure_object")].copy()
    df = df[df["distance_delta_m"].astype(float) >= MIN_VISIBLE_DISTANCE_DELTA_M].copy()
    df = df.sort_values(
        [
            "user_destination_score",
            "flood_overlap_reduction_m",
            "avoidance_overlap_zero_score",
            "visual_demo_score",
            "distance_delta_m",
        ],
        ascending=[False, False, False, False, True],
    )
    df = df.drop_duplicates(subset=["station", "station_line", "facility_name", "facility_address"])
    top5 = df.head(5).copy()
    top5["candidate_rank"] = range(1, len(top5) + 1)

    final = top5[
        (top5["station"].eq("불광"))
        & (top5["station_line"].eq("3호선"))
        & (top5["facility_name"].eq("사랑의집"))
    ]
    if final.empty:
        final = top5.head(1)
    final_record = final.iloc[0].to_dict()
    final_record["final_choice_reason"] = (
        "빗물받이 같은 인프라 객체가 아니고 실제 목적시설명이 명확하며, 불광역 first/last-mile로 자연스럽고, "
        f"baseline 침수흔적 중첩 {final_record['baseline_flood_overlap_length_m']}m가 "
        f"alternative에서 {final_record['alternative_flood_overlap_length_m']}m로 감소한다. "
        f"추가거리 {final_record['distance_delta_m']}m는 과도하지 않고, "
        f"Hausdorff 거리 {final_record['hausdorff_distance_m']}m로 지도 시연에서 경로 차이를 설명하기 쉽다."
    )
    final_record["end_to_end_connection"] = {
        "usable_as_first_mile": True,
        "usable_as_last_mile": True,
        "connectable_subway_station": f"{final_record['station']}({final_record['station_line']})",
        "example_subway_connection": "불광(3호선) → 연신내(3호선) 기존 지하철 Route A와 결합 가능",
        "note": "이번 파일은 최종 보행 후보 1개 고정이며, 전체 보행→지하철→보행 화면 연결은 다음 단계에서 별도 조립한다.",
    }
    return top5, top5.to_dict("records"), final_record


def save_geojson(top5: pd.DataFrame) -> None:
    graph = ox.load_graphml(GRAPHML_PATH)
    baselines = pd.read_csv(BASELINES_CSV, encoding="utf-8-sig").set_index("od_id")
    features = []
    for _, row in top5.iterrows():
        baseline = baselines.loc[row["od_id"]]
        for route_type, source in [("baseline", baseline), ("alternative", row)]:
            nodes = parse_nodes(source["node_sequence"])
            geom = route_linestring(graph, nodes)
            properties = {
                "candidate_rank": int(row["candidate_rank"]),
                "od_id": row["od_id"],
                "route_type": route_type,
                "station": row["station"],
                "station_line": row["station_line"],
                "facility_name": row["facility_name"],
                "facility_address": row["facility_address"],
                "route_length_m": float(source["route_length_m"]),
                "baseline_flood_overlap_m": float(row["baseline_flood_overlap_length_m"]),
                "alternative_flood_overlap_m": float(row["alternative_flood_overlap_length_m"]),
                "flood_overlap_reduction_m": float(row["flood_overlap_reduction_m"]),
                "distance_delta_m": float(row["distance_delta_m"]),
                "facility_class": row["facility_class"],
            }
            features.append({"type": "Feature", "properties": properties, "geometry": geom.__geo_interface__})
    write_json(CURATED_TOP5_GEOJSON, {"type": "FeatureCollection", "features": features})


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    top5, records, final = curate()
    write_json(CURATED_TOP5_JSON, {"candidates": records})
    write_json(FINAL_SCENARIO_JSON, final)
    save_geojson(top5)

    report = {
        "selection_policy": {
            "used_existing_207_candidates_only": True,
            "deprioritized_infrastructure_objects": INFRA_KEYWORDS,
            "deprioritized_low_priority_private_business": LOW_PRIORITY_KEYWORDS,
            "preferred_clear_user_destinations": HIGH_PRIORITY_KEYWORDS,
            "min_visible_distance_delta_m": MIN_VISIBLE_DISTANCE_DELTA_M,
            "risk_score_created": False,
            "flood_overlap_ratio_meaning": "전체 경로 중 과거 침수흔적 geometry와 공간적으로 중첩되는 길이 비율이며 침수확률이 아님",
        },
        "final_candidate": final,
        "output_files": [rel(CURATED_TOP5_JSON), rel(CURATED_TOP5_GEOJSON), rel(FINAL_SCENARIO_JSON), rel(REPORT_JSON)],
    }
    write_json(REPORT_JSON, report)

    print("## TOP 5 curated walking candidates")
    for record in records:
        print(
            f"{record['candidate_rank']}. {record['station']}({record['station_line']}) ↔ {record['facility_name']} "
            f"/ baseline={record['baseline_length_m']}m / alternative={record['alternative_length_m']}m "
            f"/ +{record['distance_delta_m']}m / overlap {record['baseline_flood_overlap_length_m']}m → "
            f"{record['alternative_flood_overlap_length_m']}m / reduction={record['flood_overlap_reduction_m']}m"
        )
    print("\n## Final fixed candidate")
    print(
        f"{final['station']}({final['station_line']}) ↔ {final['facility_name']} / "
        f"baseline={final['baseline_length_m']}m / alternative={final['alternative_length_m']}m / "
        f"+{final['distance_delta_m']}m / overlap {final['baseline_flood_overlap_length_m']}m → "
        f"{final['alternative_flood_overlap_length_m']}m"
    )
    print("\nSaved:")
    for output in report["output_files"]:
        print(output)


if __name__ == "__main__":
    main()
