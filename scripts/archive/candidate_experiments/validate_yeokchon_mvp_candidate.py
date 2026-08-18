from __future__ import annotations

import json
from itertools import islice
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
from shapely.geometry import LineString

from build_osm_walking_routing import (
    ACCESSIBILITY_FILE,
    GRAPHML_PATH,
    METRIC_CRS,
    MOBILITY_FILE,
    PROJECT_ROOT,
    STATION_MASTER_FILE,
    SUPPORT_POINTS_FILE,
    add_context_counts,
    add_flood_metrics,
    canonical_line,
    edge_tag_counts,
    flatten_actual_geometry,
    node_overlap_ratio,
    read_csv_korean,
    rel,
    route_length,
    route_linestring,
    write_json,
)
from build_targeted_osm_flood_avoidance import get_baseline_flood_edges


OUTPUT_DIR = PROJECT_ROOT / "data/processed/final_mvp/yeokchon_validation"
STATION_VALIDATION_JSON = OUTPUT_DIR / "yeokchon_station_validation.json"
DESTINATION_CANDIDATES_CSV = OUTPUT_DIR / "yeokchon_destination_candidates.csv"
BASELINE_ROUTES_CSV = OUTPUT_DIR / "yeokchon_baseline_routes.csv"
FLOOD_EXPOSED_ROUTES_CSV = OUTPUT_DIR / "yeokchon_flood_exposed_routes.csv"
ALTERNATIVE_ROUTES_CSV = OUTPUT_DIR / "yeokchon_alternative_routes.csv"
TOP_CANDIDATES_JSON = OUTPUT_DIR / "yeokchon_top_candidates.json"
TOP_CANDIDATES_GEOJSON = OUTPUT_DIR / "yeokchon_top_candidates.geojson"
COMPARISON_JSON = OUTPUT_DIR / "yeokchon_vs_bulgwang_comparison.json"
REPORT_JSON = OUTPUT_DIR / "yeokchon_validation_report.json"

TARGET_STATION = "역촌"
TARGET_LINE = "6호선"
MIN_WALKING_DISTANCE_M = 300
MAX_WALKING_DISTANCE_M = 1500
SOFT_MAX_WALKING_DISTANCE_M = 1700
MAX_SIMPLE_PATHS_PER_OD = 12
MAX_ALTERNATIVES_PER_OD = 5
MAX_ALT_LENGTH_RATIO = 2.0
MIN_DIFFERENCE_NODE_OVERLAP = 0.9

EXCLUDED_NAME_KEYWORDS = ["빗물받이", "공인중개사", "부동산", "사랑의집"]
PREFERRED_KEYWORDS = [
    "주민센터",
    "보건",
    "병원",
    "의원",
    "약국",
    "복지",
    "장애",
    "도서관",
    "센터",
    "학교",
    "어린이",
    "보호",
    "생활",
]
ADDITIONAL_CHECKS = [
    {"name": "불광1동 주민센터", "address": "서울 은평구 진흥로15길 10"},
    {"name": "불광보건분소", "address": "서울 은평구 연서로34길 11"},
]

BULGWANG_REFERENCE = {
    "station": "불광",
    "station_line": "3호선",
    "facility_name": "불광제1동주민센터",
    "baseline_length_m": 331.712,
    "alternative_length_m": 333.743,
    "distance_delta_m": 2.031,
    "baseline_flood_overlap_length_m": 9.985,
    "alternative_flood_overlap_length_m": 0.0,
    "flood_overlap_reduction_m": 9.985,
    "hausdorff_distance_m": 29.228,
}


def repair_mojibake(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        repaired = value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired if any("가" <= ch <= "힣" for ch in repaired) else value


def load_station() -> dict:
    master = read_csv_korean(STATION_MASTER_FILE)
    master["line_name"] = master["호선"].map(canonical_line)
    row = master[(master["역사명"].eq(TARGET_STATION)) & (master["line_name"].eq(TARGET_LINE))]
    if row.empty:
        raise RuntimeError(f"{TARGET_STATION} {TARGET_LINE} station master row not found.")
    row = row.iloc[0]
    return {
        "station": TARGET_STATION,
        "station_line": TARGET_LINE,
        "station_code": str(row["역사_ID"]),
        "station_lon": float(row["경도"]),
        "station_lat": float(row["위도"]),
    }


def find_accessibility() -> dict:
    processed_exists = False
    processed_rows = []
    if ACCESSIBILITY_FILE.exists():
        acc = pd.read_csv(ACCESSIBILITY_FILE, encoding="utf-8")
        if {"station_name", "line_name"}.issubset(acc.columns):
            rows = acc[(acc["station_name"].eq(TARGET_STATION)) & (acc["line_name"].eq(TARGET_LINE))]
            processed_exists = not rows.empty
            processed_rows = rows.to_dict("records")

    raw_rows = []
    for path in sorted((PROJECT_ROOT / "data/raw/accessibility").glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        def walk(value: object) -> None:
            if isinstance(value, dict):
                repaired = {key: repair_mojibake(val) for key, val in value.items()}
                if repaired.get("stnNm") == TARGET_STATION:
                    raw_rows.append(
                        {
                            "source_file": rel(path),
                            "station_name": repaired.get("stnNm"),
                            "station_code": repaired.get("stnCd"),
                            "facility_name": repaired.get("fcltNm"),
                            "location": repaired.get("dtlPstn"),
                            "oprtngSitu_raw": repaired.get("oprtngSitu"),
                        }
                    )
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(data)
        if raw_rows:
            break

    facility_names = [str(row.get("facility_name", "")) for row in raw_rows]
    return {
        "processed_accessibility_data_exists": processed_exists,
        "processed_rows": processed_rows,
        "raw_accessibility_data_exists": bool(raw_rows),
        "raw_rows": raw_rows,
        "elevator_exists_raw": any("엘리베이터" in name or "승강기" in name for name in facility_names),
        "wheelchair_lift_exists_raw": any("휠체어" in name or "리프트" in name for name in facility_names),
        "accessibility_data_missing": not processed_exists and not raw_rows,
        "note": "시설 존재 여부만 확인했으며 oprtngSitu 값은 실시간 이용 가능 여부로 해석하지 않는다.",
    }


def load_candidate_facilities() -> gpd.GeoDataFrame:
    frames = []
    for source_name, path in (
        ("mobility_facility", MOBILITY_FILE),
        ("pedestrian_support_point", SUPPORT_POINTS_FILE),
    ):
        gdf = flatten_actual_geometry(gpd.read_file(path))
        points = gdf[gdf.geometry.geom_type == "Point"].copy()
        if points.empty:
            continue
        points["facility_source"] = source_name
        points["facility_name"] = points.get("name", points.get("CONTENTS_NAME", "")).fillna("").map(repair_mojibake)
        points["facility_address"] = points.get("address_new", points.get("ADDR_NEW", "")).fillna("").map(repair_mojibake)
        fallback = points.get("address_old", points.get("ADDR_OLD", "")).fillna("").map(repair_mojibake)
        points.loc[points["facility_address"].eq(""), "facility_address"] = fallback
        points["facility_id"] = points.get("source_id", pd.Series([""] * len(points))).fillna("").astype(str)
        frames.append(points[["facility_source", "facility_id", "facility_name", "facility_address", "geometry"]])
    facilities = pd.concat(frames, ignore_index=True).pipe(gpd.GeoDataFrame, crs="EPSG:4326")
    facilities = facilities[
        facilities["facility_name"].astype(str).str.len().gt(0)
        & ~facilities["facility_name"].astype(str).apply(lambda name: any(key in name for key in EXCLUDED_NAME_KEYWORDS))
    ].copy()
    facilities["is_preferred_destination"] = facilities["facility_name"].astype(str).apply(
        lambda name: any(key in name for key in PREFERRED_KEYWORDS)
    ) | facilities["facility_address"].astype(str).apply(lambda addr: any(key in addr for key in PREFERRED_KEYWORDS))
    facilities = facilities[facilities["is_preferred_destination"]].copy()
    facilities = facilities.drop_duplicates(subset=["facility_name", "facility_address", "geometry"])
    return facilities.reset_index(drop=True)


def classify_facility(name: str) -> str:
    if "주민센터" in name:
        return "공공시설/주민센터"
    if any(key in name for key in ["보건", "병원", "의원", "약국"]):
        return "병원/보건시설"
    if any(key in name for key in ["복지", "장애", "보호", "생활가정"]):
        return "복지/돌봄시설"
    if "도서관" in name:
        return "공공시설/도서관"
    if any(key in name for key in ["학교", "어린이"]):
        return "교육/돌봄시설"
    return "생활편의/기타시설"


def build_od(graph: nx.MultiDiGraph, station: dict, facilities: gpd.GeoDataFrame) -> pd.DataFrame:
    station_node, station_snap = ox.distance.nearest_nodes(
        graph, station["station_lon"], station["station_lat"], return_dist=True
    )
    station_point = gpd.GeoSeries.from_xy([station["station_lon"]], [station["station_lat"]], crs="EPSG:4326").to_crs(
        METRIC_CRS
    ).iloc[0]
    facilities_m = facilities.to_crs(METRIC_CRS)
    rows = []
    for idx, row in facilities.iterrows():
        straight = float(station_point.distance(facilities_m.iloc[idx].geometry))
        if straight > SOFT_MAX_WALKING_DISTANCE_M:
            continue
        facility_node, facility_snap = ox.distance.nearest_nodes(
            graph, float(row.geometry.x), float(row.geometry.y), return_dist=True
        )
        rows.append(
            {
                "od_id": f"yeokchon_{idx}",
                **station,
                "facility_source": row["facility_source"],
                "facility_id": row["facility_id"],
                "facility_name": row["facility_name"],
                "facility_type": classify_facility(str(row["facility_name"])),
                "facility_address": row["facility_address"],
                "facility_lon": float(row.geometry.x),
                "facility_lat": float(row.geometry.y),
                "straight_line_distance_m": round(straight, 3),
                "station_node": int(station_node),
                "facility_node": int(facility_node),
                "station_snap_distance_m": float(station_snap),
                "facility_snap_distance_m": float(facility_snap),
                "snap_status": "PASS" if float(station_snap) <= 100 and float(facility_snap) <= 100 else "WARNING",
                "distance_filter_note": "soft_over_1500m" if straight > MAX_WALKING_DISTANCE_M else "300m_to_1500m",
            }
        )
    od = pd.DataFrame(rows)
    od = od[(od["straight_line_distance_m"] >= MIN_WALKING_DISTANCE_M) & (od["snap_status"].eq("PASS"))].copy()
    od = od[od["station_node"] != od["facility_node"]].copy()
    return od.sort_values(["straight_line_distance_m", "facility_name"]).reset_index(drop=True)


def make_route_record(row: pd.Series, route_type: str, method: str, path: list[int], graph: nx.MultiDiGraph) -> dict:
    tags = edge_tag_counts(graph, path)
    edge_sequence = [[int(u), int(v)] for u, v in zip(path[:-1], path[1:])]
    return {
        **row.to_dict(),
        "route_id": f"{row['od_id']}_{route_type}",
        "route_type": route_type,
        "method": method,
        "node_sequence": json.dumps([int(node) for node in path], ensure_ascii=False),
        "edge_sequence": json.dumps(edge_sequence, ensure_ascii=False),
        "node_count": len(path),
        "edge_count": max(0, len(path) - 1),
        "route_length_m": round(route_length(graph, path), 3),
        "steps_count": tags["steps_count"],
        "geometry": route_linestring(graph, path),
    }


def build_baselines(graph: nx.MultiDiGraph, od: pd.DataFrame) -> gpd.GeoDataFrame:
    graph_d = ox.convert.to_digraph(graph, weight="length")
    rows = []
    for _, row in od.iterrows():
        try:
            path = nx.shortest_path(graph_d, int(row["station_node"]), int(row["facility_node"]), weight="length")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        rows.append(make_route_record(row, "baseline", "shortest_path_by_length", path, graph))
    routes = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    routes = add_flood_metrics(routes)
    routes = add_context_counts(routes)
    return routes


def parse_nodes(value: object) -> list[int]:
    return [int(node) for node in json.loads(str(value))]


def build_alternatives(graph: nx.MultiDiGraph, baselines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    graph_d = ox.convert.to_digraph(graph, weight="length")
    records = []
    exposed = baselines[baselines["flood_overlap_length_m_total"].astype(float) > 0].copy()
    for _, baseline in exposed.iterrows():
        baseline_nodes = parse_nodes(baseline["node_sequence"])
        accepted = [baseline_nodes]
        try:
            paths = list(
                islice(
                    nx.shortest_simple_paths(
                        graph_d, int(baseline["station_node"]), int(baseline["facility_node"]), weight="length"
                    ),
                    MAX_SIMPLE_PATHS_PER_OD,
                )
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            paths = []
        alt_count = 0
        for path in paths[1:]:
            length = route_length(graph, path)
            if length > float(baseline["route_length_m"]) * MAX_ALT_LENGTH_RATIO:
                continue
            if any(node_overlap_ratio(path, existing) >= MIN_DIFFERENCE_NODE_OVERLAP for existing in accepted):
                continue
            alt_count += 1
            accepted.append(path)
            records.append(make_route_record(baseline.drop(labels=["geometry"]), f"k_shortest_alternative_{alt_count}", "k_shortest", path, graph))
            if alt_count >= MAX_ALTERNATIVES_PER_OD:
                break

        flood_edges = get_baseline_flood_edges(baseline, graph)
        if flood_edges:
            avoid_graph = graph_d.copy()
            avoid_graph.remove_edges_from(list(flood_edges))
            try:
                path = nx.shortest_path(
                    avoid_graph, int(baseline["station_node"]), int(baseline["facility_node"]), weight="length"
                )
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                path = None
            if path and node_overlap_ratio(path, baseline_nodes) < MIN_DIFFERENCE_NODE_OVERLAP:
                record = make_route_record(
                    baseline.drop(labels=["geometry"]),
                    "historical_flood_trace_avoidance_route",
                    "historical_flood_trace_edge_exclusion",
                    path,
                    graph,
                )
                record["excluded_baseline_flood_edge_count"] = len(flood_edges)
                records.append(record)
    if not records:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")
    routes = gpd.GeoDataFrame(records, crs="EPSG:4326")
    routes = add_flood_metrics(routes)
    routes = add_context_counts(routes)
    return routes


def compare_alternatives(baselines: gpd.GeoDataFrame, alternatives: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if alternatives.empty:
        return alternatives
    base_index = baselines.set_index("od_id")
    records = []
    for _, route in alternatives.iterrows():
        base = base_index.loc[route["od_id"]]
        distance_delta = float(route["route_length_m"]) - float(base["route_length_m"])
        overlap_reduction = float(base["flood_overlap_length_m_total"]) - float(route["flood_overlap_length_m_total"])
        base_geom, route_geom = gpd.GeoSeries([base.geometry, route.geometry], crs="EPSG:4326").to_crs(METRIC_CRS)
        record = route.drop(labels=["geometry"]).to_dict()
        record.update(
            {
                "baseline_length_m": round(float(base["route_length_m"]), 3),
                "alternative_length_m": round(float(route["route_length_m"]), 3),
                "distance_delta_m": round(distance_delta, 3),
                "distance_delta_ratio": round(distance_delta / float(base["route_length_m"]), 6)
                if float(base["route_length_m"])
                else None,
                "baseline_flood_overlap_length_m": round(float(base["flood_overlap_length_m_total"]), 3),
                "alternative_flood_overlap_length_m": round(float(route["flood_overlap_length_m_total"]), 3),
                "flood_overlap_reduction_m": round(overlap_reduction, 3),
                "flood_overlap_reduction_ratio": round(overlap_reduction / float(base["flood_overlap_length_m_total"]), 6)
                if float(base["flood_overlap_length_m_total"])
                else None,
                "baseline_flood_feature_count": int(base["flood_feature_count_unique_total"]),
                "alternative_flood_feature_count": int(route["flood_feature_count_unique_total"]),
                "hausdorff_distance_m": round(float(base_geom.hausdorff_distance(route_geom)), 3),
                "historical_flood_trace_avoiding_candidate": bool(
                    float(base["flood_overlap_length_m_total"]) > 0
                    and distance_delta >= -0.01
                    and overlap_reduction > 0.01
                    and not base.geometry.equals(route.geometry)
                ),
            }
        )
        records.append({**record, "geometry": route.geometry})
    return gpd.GeoDataFrame(records, crs="EPSG:4326")


def user_destination_reason(row: pd.Series) -> str:
    facility_type = str(row.get("facility_type", ""))
    if "주민센터" in facility_type:
        return "민원·복지 업무를 위해 일반 이용자가 실제 방문할 수 있는 공공시설이다."
    if "보건" in facility_type or "병원" in facility_type:
        return "진료·건강 관련 목적지로 방문 목적이 명확하다."
    if "복지" in facility_type:
        return "돌봄·복지 목적지로 교통약자 이동 시나리오와 연결하기 쉽다."
    if "도서관" in facility_type:
        return "공공 생활시설로 방문 목적이 명확하다."
    return "프로젝트 시설 데이터에 이름과 좌표가 있는 생활 목적지다."


def select_top(compared: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if compared.empty:
        return compared
    candidates = compared[compared["historical_flood_trace_avoiding_candidate"].eq(True)].copy()
    if candidates.empty:
        return candidates
    candidates = candidates[candidates["facility_name"].ne("사랑의집")].copy()
    candidates["demo_visual_note"] = candidates["hausdorff_distance_m"].apply(
        lambda value: "지도상 차이 보임 가능성 높음" if float(value) >= 30 else "지도상 차이 작을 수 있음"
    )
    candidates["user_destination_reason"] = candidates.apply(user_destination_reason, axis=1)
    candidates["destination_priority"] = candidates["facility_type"].map(
        {
            "공공시설/주민센터": 1,
            "병원/보건시설": 2,
            "복지/돌봄시설": 3,
            "공공시설/도서관": 4,
            "교육/돌봄시설": 5,
        }
    ).fillna(6)
    candidates = candidates.sort_values(
        [
            "destination_priority",
            "flood_overlap_reduction_m",
            "hausdorff_distance_m",
            "distance_delta_ratio",
        ],
        ascending=[True, False, False, True],
    )
    return candidates.drop_duplicates(subset=["facility_name", "facility_address"]).head(5)


def save_table(gdf: gpd.GeoDataFrame, path: Path) -> None:
    table = pd.DataFrame(gdf.drop(columns=["geometry"], errors="ignore"))
    table.to_csv(path, index=False, encoding="utf-8-sig")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not GRAPHML_PATH.exists():
        raise RuntimeError(f"OSM graph not found: {rel(GRAPHML_PATH)}")

    graph = ox.load_graphml(GRAPHML_PATH)
    station = load_station()
    accessibility = find_accessibility()
    write_json(STATION_VALIDATION_JSON, {**station, "accessibility": accessibility})

    facilities = load_candidate_facilities()
    od = build_od(graph, station, facilities)
    od.to_csv(DESTINATION_CANDIDATES_CSV, index=False, encoding="utf-8-sig")

    baselines = build_baselines(graph, od)
    save_table(baselines, BASELINE_ROUTES_CSV)
    exposed = baselines[baselines["flood_overlap_length_m_total"].astype(float) > 0].copy()
    save_table(exposed, FLOOD_EXPOSED_ROUTES_CSV)

    alternatives = build_alternatives(graph, baselines)
    compared = compare_alternatives(baselines, alternatives)
    save_table(compared, ALTERNATIVE_ROUTES_CSV)

    top = select_top(compared)
    top_records = []
    if not top.empty:
        top_for_geojson = top.copy()
        top_for_geojson.to_file(TOP_CANDIDATES_GEOJSON, driver="GeoJSON")
        wanted = [
            "facility_name",
            "facility_type",
            "facility_address",
            "facility_lon",
            "facility_lat",
            "baseline_length_m",
            "alternative_length_m",
            "distance_delta_m",
            "baseline_flood_overlap_length_m",
            "alternative_flood_overlap_length_m",
            "flood_overlap_reduction_m",
            "flood_overlap_reduction_ratio",
            "hausdorff_distance_m",
            "demo_visual_note",
            "user_destination_reason",
            "method",
        ]
        top_records = top[wanted].to_dict("records")
    else:
        gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326").to_file(TOP_CANDIDATES_GEOJSON, driver="GeoJSON")
    write_json(TOP_CANDIDATES_JSON, top_records)

    top1 = top_records[0] if top_records else None
    if top1:
        clearly_better_dimensions = [
            "목적지가 공공/보건/복지시설로 명확함"
            if top1["facility_type"] in {"공공시설/주민센터", "병원/보건시설", "복지/돌봄시설"}
            else None,
            "침수흔적 감소량이 불광 기준보다 큼"
            if top1["flood_overlap_reduction_m"] > BULGWANG_REFERENCE["flood_overlap_reduction_m"] + 0.01
            else None,
            "지도상 분리 지표가 불광 기준보다 큼"
            if top1["hausdorff_distance_m"] > BULGWANG_REFERENCE["hausdorff_distance_m"] + 5
            else None,
        ]
        clearly_better_dimensions = [item for item in clearly_better_dimensions if item]
    else:
        clearly_better_dimensions = []

    if not top1:
        case = "CASE C"
        final_line = "불광역 3호선 → 불광제1동주민센터를 최종 MVP 후보로 유지 추천"
    elif len(clearly_better_dimensions) >= 2:
        case = "CASE A"
        final_line = "역촌역 후보가 충분히 우수하므로 최종 MVP 변경 검토 가능"
    else:
        case = "CASE B"
        final_line = "불광역 3호선 → 불광제1동주민센터를 최종 MVP 후보로 유지 추천"

    comparison = {
        "bulgwang_reference": BULGWANG_REFERENCE,
        "yeokchon_top1": top1,
        "clearly_better_dimensions": clearly_better_dimensions,
        "case": case,
        "final_recommendation": final_line,
    }
    write_json(COMPARISON_JSON, comparison)

    additional_checks = []
    for item in ADDITIONAL_CHECKS:
        matched = od[
            od["facility_name"].astype(str).str.contains(item["name"].replace("1동", "제1동"), na=False)
            | od["facility_address"].astype(str).str.contains(item["address"].replace("서울 은평구", "서울특별시 은평구"), na=False)
        ]
        additional_checks.append(
            {
                **item,
                "found_in_project_facility_data": bool(len(matched)),
                "matched_count_in_candidate_scope": int(len(matched)),
            }
        )

    report = {
        "station_validation": {**station, "accessibility": accessibility},
        "candidate_count": int(len(od)),
        "baseline_routing_success_count": int(len(baselines)),
        "flood_overlap_gt_0_count": int(len(exposed)),
        "alternative_route_count": int(len(compared)),
        "actual_overlap_reduction_alternative_count": int(
            compared["historical_flood_trace_avoiding_candidate"].sum() if not compared.empty else 0
        ),
        "top_candidate_count": int(len(top_records)),
        "additional_checks": additional_checks,
        "comparison_case": case,
        "final_recommendation": final_line,
        "note": "flood_overlap_ratio는 침수확률이 아니라 전체 경로 길이 중 2022~2025 과거 침수흔적 geometry와 공간적으로 중첩되는 길이 비율이다.",
        "output_files": [
            rel(STATION_VALIDATION_JSON),
            rel(DESTINATION_CANDIDATES_CSV),
            rel(BASELINE_ROUTES_CSV),
            rel(FLOOD_EXPOSED_ROUTES_CSV),
            rel(ALTERNATIVE_ROUTES_CSV),
            rel(TOP_CANDIDATES_JSON),
            rel(TOP_CANDIDATES_GEOJSON),
            rel(COMPARISON_JSON),
            rel(REPORT_JSON),
        ],
    }
    write_json(REPORT_JSON, report)

    print("1. 역촌역 좌표/6호선 매칭 여부")
    print(f"- matched: True, station_code: {station['station_code']}, lon: {station['station_lon']}, lat: {station['station_lat']}")
    print("2. 접근성 데이터 존재 여부")
    print(
        "- processed_accessibility_data_exists: "
        f"{accessibility['processed_accessibility_data_exists']}, raw_accessibility_data_exists: {accessibility['raw_accessibility_data_exists']}, "
        f"elevator_exists_raw: {accessibility['elevator_exists_raw']}, wheelchair_lift_exists_raw: {accessibility['wheelchair_lift_exists_raw']}"
    )
    print("3. 목적지 후보 수")
    print(f"- {len(od)}")
    print("4. baseline routing 성공 수")
    print(f"- {len(baselines)}")
    print("5. flood overlap > 0 후보 수")
    print(f"- {len(exposed)}")
    print("6. 실제 overlap 감소 alternative 수")
    print(f"- {report['actual_overlap_reduction_alternative_count']}")
    print("7. 역촌역 TOP 후보 최대 5개")
    if not top_records:
        print("- 조건을 만족하는 TOP 후보 없음")
    else:
        for idx, row in enumerate(top_records, start=1):
            print(
                f"- {idx}. {row['facility_name']} ({row['facility_type']}): "
                f"baseline {row['baseline_length_m']}m, alternative {row['alternative_length_m']}m, "
                f"+{row['distance_delta_m']}m, overlap {row['baseline_flood_overlap_length_m']}m -> "
                f"{row['alternative_flood_overlap_length_m']}m, reduction {row['flood_overlap_reduction_m']}m, "
                f"Hausdorff {row['hausdorff_distance_m']}m"
            )
    print("8. 불광역 주민센터 후보와 비교")
    if top1:
        print(
            f"- 역촌 TOP1 {top1['facility_name']}: reduction {top1['flood_overlap_reduction_m']}m, "
            f"extra +{top1['distance_delta_m']}m, Hausdorff {top1['hausdorff_distance_m']}m"
        )
    else:
        print("- 역촌 TOP1 없음")
    print(
        "- 불광 기준 불광제1동주민센터: reduction "
        f"{BULGWANG_REFERENCE['flood_overlap_reduction_m']}m, extra +{BULGWANG_REFERENCE['distance_delta_m']}m, "
        f"Hausdorff {BULGWANG_REFERENCE['hausdorff_distance_m']}m"
    )
    print("9. 최종 판단: " + case)
    print(final_line)


if __name__ == "__main__":
    main()
