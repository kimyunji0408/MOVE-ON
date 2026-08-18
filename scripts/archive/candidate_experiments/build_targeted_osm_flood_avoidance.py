from __future__ import annotations

import json
from itertools import islice
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd

from build_osm_walking_routing import (
    GRAPHML_PATH,
    METRIC_CRS,
    PROJECT_ROOT,
    YEARS,
    add_context_counts,
    add_flood_metrics,
    edge_tag_counts,
    node_overlap_ratio,
    rel,
    route_length,
    route_linestring,
    write_json,
)


SOURCE_DIR = PROJECT_ROOT / "data/processed/osm_routing"
OUTPUT_DIR = PROJECT_ROOT / "data/processed/osm_routing_targeted"

EXISTING_ROUTES_CSV = SOURCE_DIR / "osm_walking_routes.csv"
OD_CANDIDATES_CSV = SOURCE_DIR / "station_facility_od_candidates.csv"

ALL_BASELINE_CSV = OUTPUT_DIR / "all_baseline_routes.csv"
ALL_BASELINE_EXPOSURE_CSV = OUTPUT_DIR / "all_baseline_flood_exposure.csv"
BASELINE_EXPOSED_CSV = OUTPUT_DIR / "baseline_flood_exposed_od_candidates.csv"
TARGETED_ALTERNATIVES_CSV = OUTPUT_DIR / "targeted_route_alternatives.csv"
AVOIDANCE_ROUTES_CSV = OUTPUT_DIR / "historical_flood_trace_avoidance_routes.csv"
TOP_JSON = OUTPUT_DIR / "top_targeted_mvp_candidates.json"
TOP_GEOJSON = OUTPUT_DIR / "top_targeted_mvp_candidates.geojson"
REPORT_JSON = OUTPUT_DIR / "targeted_routing_analysis_report.json"

MAX_EXPOSED_OD_FOR_ALTERNATIVES = 100
MAX_SIMPLE_PATHS_PER_OD = 12
MAX_ALTERNATIVES_PER_OD = 5
MAX_ALT_LENGTH_RATIO = 2.0
MIN_DIFFERENCE_NODE_OVERLAP = 0.9


def load_existing_sample_diagnosis() -> dict:
    routes = pd.read_csv(EXISTING_ROUTES_CSV, encoding="utf-8")
    baseline = routes[routes["route_type"] == "baseline"].copy()
    baseline["flood_overlap_length_m_total"] = pd.to_numeric(
        baseline["flood_overlap_length_m_total"], errors="coerce"
    ).fillna(0)
    baseline["flood_feature_count_unique_total"] = pd.to_numeric(
        baseline["flood_feature_count_unique_total"], errors="coerce"
    ).fillna(0)
    exposed = baseline[baseline["flood_overlap_length_m_total"] > 0].copy()
    top10 = exposed.sort_values(
        ["flood_overlap_length_m_total", "flood_overlap_ratio", "flood_feature_count_unique_total"],
        ascending=[False, False, False],
    ).head(10)
    return {
        "baseline_total": int(len(baseline)),
        "baseline_overlap_gt_0_count": int(len(exposed)),
        "baseline_overlap_eq_0_count": int((baseline["flood_overlap_length_m_total"] == 0).sum()),
        "baseline_flood_feature_count_gt_0_count": int((baseline["flood_feature_count_unique_total"] > 0).sum()),
        "top10": top10[
            [
                "od_id",
                "station",
                "station_line",
                "facility_name",
                "route_length_m",
                "flood_overlap_length_m_total",
                "flood_overlap_ratio",
                "flood_feature_count_unique_total",
            ]
        ].to_dict("records"),
        "diagnosis": (
            "기존 80개 샘플 중 flood overlap > 0 baseline이 적어 침수노출 경로를 충분히 포함하지 않았다."
            if len(exposed) < max(1, len(baseline) * 0.1)
            else "기존 80개 샘플에도 flood overlap baseline이 일정 비율 포함되었다."
        ),
    }


def load_graph() -> nx.MultiDiGraph:
    if not GRAPHML_PATH.exists():
        raise RuntimeError(f"OSM GraphML not found: {GRAPHML_PATH}")
    return ox.load_graphml(GRAPHML_PATH)


def load_od_candidates() -> pd.DataFrame:
    od = pd.read_csv(OD_CANDIDATES_CSV, encoding="utf-8")
    required = {"station_node", "facility_node", "snap_status"}
    missing = required - set(od.columns)
    if missing:
        raise RuntimeError(f"OD candidate file is missing snap columns: {sorted(missing)}")
    od = od[od["snap_status"] == "PASS"].copy()
    od["station_node"] = pd.to_numeric(od["station_node"], errors="coerce").astype("Int64")
    od["facility_node"] = pd.to_numeric(od["facility_node"], errors="coerce").astype("Int64")
    od = od.dropna(subset=["station_node", "facility_node"])
    od = od[od["station_node"] != od["facility_node"]].copy()
    return od.reset_index(drop=True)


def make_route_record(row: pd.Series, route_type: str, path: list[int], graph: nx.MultiDiGraph) -> dict:
    tags = edge_tag_counts(graph, path)
    edge_sequence = [[int(u), int(v)] for u, v in zip(path[:-1], path[1:])]
    return {
        **row.drop(labels=["geometry"], errors="ignore").to_dict(),
        "route_id": f"{row['od_id']}_{route_type}",
        "route_type": route_type,
        "node_sequence": json.dumps([int(node) for node in path], ensure_ascii=False),
        "edge_sequence": json.dumps(edge_sequence, ensure_ascii=False),
        "node_count": len(path),
        "edge_count": max(0, len(path) - 1),
        "route_length_m": round(route_length(graph, path), 3),
        "steps_count": tags["steps_count"],
        "geometry": route_linestring(graph, path),
    }


def build_all_baselines(graph: nx.MultiDiGraph, od: pd.DataFrame) -> gpd.GeoDataFrame:
    graph_d = ox.convert.to_digraph(graph, weight="length")
    rows = []
    grouped = od.groupby("station_node", sort=False)
    for source, group in grouped:
        try:
            _, paths = nx.single_source_dijkstra(graph_d, int(source), weight="length")
        except nx.NodeNotFound:
            continue
        for _, row in group.iterrows():
            target = int(row["facility_node"])
            if target not in paths:
                continue
            rows.append(make_route_record(row, "baseline", paths[target], graph))
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def save_baselines(routes: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    routes = add_flood_metrics(routes)
    route_table = pd.DataFrame(routes.drop(columns=["geometry"]))
    route_table.to_csv(ALL_BASELINE_CSV, index=False, encoding="utf-8-sig")
    exposure_cols = [
        "od_id",
        "station",
        "station_line",
        "facility_name",
        "facility_source",
        "facility_address",
        "route_length_m",
        "flood_feature_count_2022",
        "flood_feature_count_2023",
        "flood_feature_count_2024",
        "flood_feature_count_2025",
        "flood_feature_count_unique_total",
        "flood_overlap_length_m_2022",
        "flood_overlap_length_m_2023",
        "flood_overlap_length_m_2024",
        "flood_overlap_length_m_2025",
        "flood_overlap_length_m_total",
        "flood_overlap_ratio",
        "node_sequence",
        "edge_sequence",
    ]
    route_table[exposure_cols].to_csv(ALL_BASELINE_EXPOSURE_CSV, index=False, encoding="utf-8-sig")
    exposed = route_table[route_table["flood_overlap_length_m_total"] > 0].sort_values(
        ["flood_overlap_length_m_total", "flood_overlap_ratio", "flood_feature_count_unique_total"],
        ascending=[False, False, False],
    )
    exposed.to_csv(BASELINE_EXPOSED_CSV, index=False, encoding="utf-8-sig")
    return routes, exposed


def parse_nodes(node_sequence: str) -> list[int]:
    return [int(node) for node in json.loads(node_sequence)]


def generate_k_shortest_alternatives(
    graph: nx.MultiDiGraph, exposed: pd.DataFrame
) -> gpd.GeoDataFrame:
    graph_d = ox.convert.to_digraph(graph, weight="length")
    rows = []
    for _, row in exposed.head(MAX_EXPOSED_OD_FOR_ALTERNATIVES).iterrows():
        source = int(row["station_node"])
        target = int(row["facility_node"])
        baseline_nodes = parse_nodes(row["node_sequence"])
        accepted = [baseline_nodes]
        try:
            paths = list(islice(nx.shortest_simple_paths(graph_d, source, target, weight="length"), MAX_SIMPLE_PATHS_PER_OD))
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        alt_count = 0
        for path in paths[1:]:
            length = route_length(graph, path)
            if row["route_length_m"] and length > float(row["route_length_m"]) * MAX_ALT_LENGTH_RATIO:
                continue
            if any(node_overlap_ratio(path, existing) >= MIN_DIFFERENCE_NODE_OVERLAP for existing in accepted):
                continue
            alt_count += 1
            accepted.append(path)
            rows.append(make_route_record(row, f"k_shortest_alternative_{alt_count}", path, graph))
            if alt_count >= MAX_ALTERNATIVES_PER_OD:
                break
    if not rows:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")
    routes = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    routes = add_flood_metrics(routes)
    routes = add_context_counts(routes)
    return routes


def get_baseline_flood_edges(row: pd.Series, graph: nx.MultiDiGraph) -> set[tuple[int, int]]:
    nodes = parse_nodes(row["node_sequence"])
    baseline_edges = list(zip(nodes[:-1], nodes[1:]))
    edge_routes = []
    for u, v in baseline_edges:
        edge_routes.append(
            {
                "u": int(u),
                "v": int(v),
                "route_length_m": route_length(graph, [int(u), int(v)]),
                "geometry": route_linestring(graph, [int(u), int(v)]),
            }
        )
    edge_gdf = gpd.GeoDataFrame(edge_routes, crs="EPSG:4326")
    if edge_gdf.empty:
        return set()
    edge_gdf = add_flood_metrics(edge_gdf)
    flooded = edge_gdf[edge_gdf["flood_overlap_length_m_total"] > 0]
    return {(int(r["u"]), int(r["v"])) for _, r in flooded.iterrows()}


def generate_avoidance_routes(graph: nx.MultiDiGraph, exposed: pd.DataFrame) -> gpd.GeoDataFrame:
    graph_d = ox.convert.to_digraph(graph, weight="length")
    rows = []
    for _, row in exposed.head(MAX_EXPOSED_OD_FOR_ALTERNATIVES).iterrows():
        flood_edges = get_baseline_flood_edges(row, graph)
        if not flood_edges:
            continue
        g_avoid = graph_d.copy()
        g_avoid.remove_edges_from(list(flood_edges))
        try:
            path = nx.shortest_path(g_avoid, int(row["station_node"]), int(row["facility_node"]), weight="length")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        baseline_nodes = parse_nodes(row["node_sequence"])
        if node_overlap_ratio(path, baseline_nodes) >= MIN_DIFFERENCE_NODE_OVERLAP:
            continue
        record = make_route_record(row, "historical_flood_trace_avoidance_route", path, graph)
        record["excluded_baseline_flood_edge_count"] = len(flood_edges)
        rows.append(record)
    if not rows:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")
    routes = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    routes = add_flood_metrics(routes)
    routes = add_context_counts(routes)
    return routes


def compare_to_baseline(baselines: pd.DataFrame, routes: gpd.GeoDataFrame, method: str) -> gpd.GeoDataFrame:
    if routes.empty:
        return routes
    baseline_by_od = baselines.set_index("od_id")
    records = []
    for idx, route in routes.iterrows():
        base = baseline_by_od.loc[route["od_id"]]
        distance_delta = float(route["route_length_m"]) - float(base["route_length_m"])
        overlap_reduction = float(base["flood_overlap_length_m_total"]) - float(route["flood_overlap_length_m_total"])
        record = route.drop(labels=["geometry"]).to_dict()
        record.update(
            {
                "method": method,
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
                "historical_flood_trace_avoiding_candidate": bool(
                    float(base["flood_overlap_length_m_total"]) > 0
                    and distance_delta >= -0.01
                    and overlap_reduction > 0.01
                ),
            }
        )
        records.append({**record, "geometry": route.geometry})
    return gpd.GeoDataFrame(records, crs="EPSG:4326")


def save_route_outputs(k_routes: gpd.GeoDataFrame, avoid_routes: gpd.GeoDataFrame, baselines_gdf: gpd.GeoDataFrame) -> list[dict]:
    if k_routes.empty:
        pd.DataFrame().to_csv(TARGETED_ALTERNATIVES_CSV, index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame(k_routes.drop(columns=["geometry"])).to_csv(TARGETED_ALTERNATIVES_CSV, index=False, encoding="utf-8-sig")

    if avoid_routes.empty:
        pd.DataFrame().to_csv(AVOIDANCE_ROUTES_CSV, index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame(avoid_routes.drop(columns=["geometry"])).to_csv(AVOIDANCE_ROUTES_CSV, index=False, encoding="utf-8-sig")

    combined = pd.concat([k_routes, avoid_routes], ignore_index=True)
    if combined.empty:
        write_json(TOP_JSON, {"candidates": []})
        gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326").to_file(TOP_GEOJSON, driver="GeoJSON")
        return []

    strong = combined[combined["historical_flood_trace_avoiding_candidate"] == True].copy()  # noqa: E712
    if strong.empty:
        write_json(TOP_JSON, {"candidates": []})
        gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326").to_file(TOP_GEOJSON, driver="GeoJSON")
        return []

    strong["avoidance_overlap_zero"] = strong["alternative_flood_overlap_length_m"].astype(float).le(0.01)
    strong = strong.sort_values(
        ["flood_overlap_reduction_m", "avoidance_overlap_zero", "distance_delta_m"],
        ascending=[False, False, True],
    )
    deduped_rows = []
    seen = set()
    for _, row in strong.iterrows():
        key = (
            row["station"],
            row["station_line"],
            int(row["station_node"]),
            int(row["facility_node"]),
            row["node_sequence"],
        )
        if key in seen:
            continue
        seen.add(key)
        deduped_rows.append(row)
        if len(deduped_rows) >= 10:
            break
    top = gpd.GeoDataFrame(deduped_rows, crs="EPSG:4326")
    top["candidate_rank"] = range(1, len(top) + 1)

    top_features = []
    baseline_index = baselines_gdf.set_index("od_id")
    for _, row in top.iterrows():
        base = baseline_index.loc[row["od_id"]]
        top_features.append(
            {
                "od_id": row["od_id"],
                "route_type": "baseline",
                "station": row["station"],
                "facility": row["facility_name"],
                "route_length_m": base["route_length_m"],
                "flood_overlap_length_m": base["flood_overlap_length_m_total"],
                "flood_overlap_ratio": base["flood_overlap_ratio"],
                "flood_feature_count": base["flood_feature_count_unique_total"],
                "distance_delta_m": 0,
                "flood_overlap_reduction_m": 0,
                "candidate_rank": row["candidate_rank"],
                "geometry": base.geometry,
            }
        )
        top_features.append(
            {
                "od_id": row["od_id"],
                "route_type": row["route_type"],
                "station": row["station"],
                "facility": row["facility_name"],
                "route_length_m": row["route_length_m"],
                "flood_overlap_length_m": row["alternative_flood_overlap_length_m"],
                "flood_overlap_ratio": row["flood_overlap_ratio"],
                "flood_feature_count": row["flood_feature_count_unique_total"],
                "distance_delta_m": row["distance_delta_m"],
                "flood_overlap_reduction_m": row["flood_overlap_reduction_m"],
                "candidate_rank": row["candidate_rank"],
                "geometry": row.geometry,
            }
        )
    top_gdf = gpd.GeoDataFrame(top_features, crs="EPSG:4326")
    top_gdf.to_file(TOP_GEOJSON, driver="GeoJSON")

    records = top.drop(columns=["geometry"]).to_dict("records")
    write_json(TOP_JSON, {"candidates": records})
    return records


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sample = load_existing_sample_diagnosis()
    graph = load_graph()
    od = load_od_candidates()

    baselines_gdf = build_all_baselines(graph, od)
    baselines_gdf, exposed = save_baselines(baselines_gdf)
    exposed_for_alt = exposed.head(MAX_EXPOSED_OD_FOR_ALTERNATIVES)

    k_routes = generate_k_shortest_alternatives(graph, exposed_for_alt)
    k_routes = compare_to_baseline(exposed, k_routes, "k_shortest")

    avoid_routes = generate_avoidance_routes(graph, exposed_for_alt)
    avoid_routes = compare_to_baseline(exposed, avoid_routes, "historical_flood_trace_edge_exclusion")

    top = save_route_outputs(k_routes, avoid_routes, baselines_gdf)
    combined = pd.concat([pd.DataFrame(k_routes.drop(columns=["geometry"], errors="ignore")), pd.DataFrame(avoid_routes.drop(columns=["geometry"], errors="ignore"))], ignore_index=True)
    reduction_count = int(combined["historical_flood_trace_avoiding_candidate"].sum()) if not combined.empty and "historical_flood_trace_avoiding_candidate" in combined else 0
    avoid_od_count = int(avoid_routes["od_id"].nunique()) if not avoid_routes.empty else 0
    k_reduction_count = int(k_routes["historical_flood_trace_avoiding_candidate"].sum()) if not k_routes.empty else 0

    top10_baselines = exposed.head(10)[
        [
            "od_id",
            "station",
            "station_line",
            "facility_name",
            "route_length_m",
            "flood_overlap_length_m_total",
            "flood_overlap_ratio",
            "flood_feature_count_unique_total",
        ]
    ].to_dict("records")

    case = "CASE_A" if top else ("CASE_B" if len(exposed) > 0 else "CASE_C")
    judgement = {
        "CASE_A": "침수흔적 중첩 감소 경로가 발견되어 설명력 높은 MVP 후보를 추천할 수 있다.",
        "CASE_B": "flood-exposed baseline은 있지만 현재 검증한 회피경로에서 중첩 감소 후보가 없다.",
        "CASE_C": "전체 baseline 중 flood overlap 자체가 거의 없어 flood geometry와 routing network의 공간 관계 또는 데모 지역 재검토가 필요하다.",
    }[case]

    report = {
        "generated_at": pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d %H:%M:%S"),
        "case": case,
        "final_judgement": judgement,
        "existing_80_sample_diagnosis": sample,
        "baseline_sweep": {
            "candidate_od_total": int(len(od)),
            "routing_success": int(len(baselines_gdf)),
            "routing_failure": int(len(od) - len(baselines_gdf)),
            "flood_overlap_gt_0_od_count": int(len(exposed)),
            "flood_overlap_gt_0_ratio": round(len(exposed) / len(baselines_gdf), 6) if len(baselines_gdf) else 0,
            "top10": top10_baselines,
        },
        "avoidance_search": {
            "analyzed_flood_exposed_od_count": int(len(exposed_for_alt)),
            "k_shortest_reduction_candidate_count": k_reduction_count,
            "flood_edge_exclusion_route_exists_od_count": avoid_od_count,
            "actual_overlap_reduction_candidate_count": reduction_count,
            "max_overlap_reduction_m": max((float(c["flood_overlap_reduction_m"]) for c in top), default=None),
        },
        "limits": [
            "flood_overlap_ratio는 침수확률이 아니라 전체 보행경로 중 과거 침수흔적 geometry와 공간적으로 중첩되는 길이 비율이다.",
            "회피경로는 현재 침수된 도로를 피한다는 의미가 아니라, 과거 침수흔적과 중첩된 baseline edge를 제외해도 OSM 경로가 존재하는지 검증한 실험이다.",
            "Risk Score, 강수 임계값, MFP, LAD, Alan은 구현하지 않았다.",
        ],
        "output_files": [
            rel(ALL_BASELINE_CSV),
            rel(ALL_BASELINE_EXPOSURE_CSV),
            rel(BASELINE_EXPOSED_CSV),
            rel(TARGETED_ALTERNATIVES_CSV),
            rel(AVOIDANCE_ROUTES_CSV),
            rel(TOP_JSON),
            rel(TOP_GEOJSON),
            rel(REPORT_JSON),
        ],
    }
    write_json(REPORT_JSON, report)

    print("## 1. 기존 80개 샘플 진단")
    print(f"* baseline overlap > 0 수: {sample['baseline_overlap_gt_0_count']}")
    print(f"* overlap = 0 수: {sample['baseline_overlap_eq_0_count']}")
    print(f"* 기존 샘플에서 감소 후보가 없었던 주요 이유: {sample['diagnosis']}")

    print("\n## 2. 전체 baseline sweep")
    print(f"* 전체 후보 OD: {len(od)}")
    print(f"* routing 성공: {len(baselines_gdf)}")
    print(f"* routing 실패: {len(od) - len(baselines_gdf)}")
    print(f"* flood overlap > 0 OD 수: {len(exposed)}")
    print(f"* 전체 대비 비율: {report['baseline_sweep']['flood_overlap_gt_0_ratio']}")

    print("\n## 3. 가장 침수흔적 중첩이 큰 baseline TOP 10")
    for row in top10_baselines:
        print(
            f"* {row['station']}({row['station_line']}) ↔ {row['facility_name']}: "
            f"거리={row['route_length_m']}m, overlap={row['flood_overlap_length_m_total']}m, "
            f"ratio={row['flood_overlap_ratio']}, unique flood={row['flood_feature_count_unique_total']}"
        )

    print("\n## 4. 회피경로 탐색")
    print(f"* 분석한 flood-exposed OD 수: {len(exposed_for_alt)}")
    print(f"* k-shortest reduction 후보 수: {k_reduction_count}")
    print(f"* flood-edge exclusion 방식에서 회피경로 존재 OD 수: {avoid_od_count}")
    print(f"* 실제 overlap 감소 후보 수: {reduction_count}")

    print("\n## 5. MOVE:ON TOP 10")
    if top:
        for row in top:
            print(
                f"* {row['station']}({row['station_line']}) ↔ {row['facility_name']}: "
                f"baseline={row['baseline_length_m']}m, 회피={row['alternative_length_m']}m, "
                f"추가={row['distance_delta_m']}m, baseline overlap={row['baseline_flood_overlap_length_m']}m, "
                f"회피 overlap={row['alternative_flood_overlap_length_m']}m, 감소={row['flood_overlap_reduction_m']}m, "
                f"감소율={row['flood_overlap_reduction_ratio']}"
            )
    else:
        print("* TOP 후보 없음")

    print("\n## 6. 최종 판단")
    print(judgement)

    print("\nSaved:")
    for output in report["output_files"]:
        print(output)


if __name__ == "__main__":
    main()
