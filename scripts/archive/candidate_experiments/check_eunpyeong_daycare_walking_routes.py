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
    MOBILITY_FILE,
    PROJECT_ROOT,
    add_context_counts,
    add_flood_metrics,
    load_stations,
    node_overlap_ratio,
    rel,
    route_length,
    route_linestring,
    write_json,
)
from build_targeted_osm_flood_avoidance import get_baseline_flood_edges


OUTPUT_DIR = PROJECT_ROOT / "data/processed/final_mvp/eunpyeong_daycare_routes"
ROUTES_CSV = OUTPUT_DIR / "station_to_eunpyeong_daycare_routes.csv"
ROUTES_JSON = OUTPUT_DIR / "station_to_eunpyeong_daycare_routes.json"
ROUTES_GEOJSON = OUTPUT_DIR / "station_to_eunpyeong_daycare_routes.geojson"
REPORT_JSON = OUTPUT_DIR / "station_to_eunpyeong_daycare_report.json"

DESTINATION_NAME = "은평장애인주간보호센터"
DESTINATION_ADDRESS = "서울특별시 은평구 진흥로 87"
TARGET_STATION_NAMES = ["녹번", "불광", "독바위", "연신내", "응암"]
MAX_ALTERNATIVES_PER_OD = 5
MAX_SIMPLE_PATHS_PER_OD = 12
MAX_ALT_LENGTH_RATIO = 2.0
MIN_DIFFERENCE_NODE_OVERLAP = 0.9


def load_destination() -> dict:
    facilities = gpd.read_file(MOBILITY_FILE)
    rows = facilities[
        facilities["name"].fillna("").eq(DESTINATION_NAME)
        | facilities["CONTENTS_NAME"].fillna("").eq(DESTINATION_NAME)
        | facilities["address_new"].fillna("").eq(DESTINATION_ADDRESS)
        | facilities["ADDR_NEW"].fillna("").eq(DESTINATION_ADDRESS)
    ].copy()
    if rows.empty:
        raise RuntimeError(f"Destination not found in processed mobility data: {DESTINATION_NAME}")
    row = rows.iloc[0]
    geom = row.geometry
    if geom.geom_type == "GeometryCollection":
        points = [g for g in geom.geoms if g.geom_type == "Point"]
        if not points:
            raise RuntimeError("Destination feature has no Point geometry.")
        geom = points[0]
    if geom.geom_type != "Point":
        raise RuntimeError(f"Destination geometry is not Point: {geom.geom_type}")
    return {
        "facility_id": str(row.get("source_id", "")),
        "facility_name": DESTINATION_NAME,
        "facility_address": DESTINATION_ADDRESS,
        "facility_lon": float(geom.x),
        "facility_lat": float(geom.y),
    }


def make_station_od_rows(graph: nx.MultiDiGraph, destination: dict) -> pd.DataFrame:
    stations = load_stations()
    stations = stations[stations["station"].isin(TARGET_STATION_NAMES)].copy()
    stations = stations.sort_values(["station", "line"]).reset_index(drop=True)
    facility_node, facility_snap_distance = ox.distance.nearest_nodes(
        graph, destination["facility_lon"], destination["facility_lat"], return_dist=True
    )
    rows = []
    station_points = gpd.GeoDataFrame(stations, geometry=stations.geometry, crs="EPSG:4326").to_crs("EPSG:5186")
    dest_point = gpd.GeoSeries(
        [gpd.points_from_xy([destination["facility_lon"]], [destination["facility_lat"]], crs="EPSG:4326")[0]],
        crs="EPSG:4326",
    ).to_crs("EPSG:5186").iloc[0]
    for idx, station in stations.iterrows():
        station_node, station_snap_distance = ox.distance.nearest_nodes(
            graph, station["lon"], station["lat"], return_dist=True
        )
        straight = float(station_points.iloc[idx].geometry.distance(dest_point))
        rows.append(
            {
                "od_id": f"{station['station']}_{station['line']}_to_eunpyeong_daycare",
                "station": station["station"],
                "station_line": station["line"],
                "station_lon": float(station["lon"]),
                "station_lat": float(station["lat"]),
                "facility_source": "mobility_facility",
                **destination,
                "straight_line_distance_m": round(straight, 3),
                "station_node": int(station_node),
                "facility_node": int(facility_node),
                "station_snap_distance_m": float(station_snap_distance),
                "facility_snap_distance_m": float(facility_snap_distance),
                "snap_status": "PASS"
                if float(station_snap_distance) <= 100 and float(facility_snap_distance) <= 100
                else "WARNING",
            }
        )
    return pd.DataFrame(rows)


def make_route_record(row: pd.Series, route_type: str, method: str, path: list[int], graph: nx.MultiDiGraph) -> dict:
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
        "geometry": route_linestring(graph, path),
    }


def parse_nodes(value: object) -> list[int]:
    return [int(node) for node in json.loads(str(value))]


def generate_baselines(graph: nx.MultiDiGraph, od_rows: pd.DataFrame) -> gpd.GeoDataFrame:
    graph_d = ox.convert.to_digraph(graph, weight="length")
    records = []
    for _, row in od_rows.iterrows():
        try:
            path = nx.shortest_path(graph_d, int(row["station_node"]), int(row["facility_node"]), weight="length")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        records.append(make_route_record(row, "baseline", "shortest_path_by_length", path, graph))
    routes = gpd.GeoDataFrame(records, crs="EPSG:4326")
    routes = add_flood_metrics(routes)
    routes = add_context_counts(routes)
    return routes


def generate_alternatives_for_exposed(graph: nx.MultiDiGraph, baselines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
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
                if node_overlap_ratio(path, baseline_nodes) < MIN_DIFFERENCE_NODE_OVERLAP:
                    record = make_route_record(
                        baseline.drop(labels=["geometry"]),
                        "historical_flood_trace_avoidance_route",
                        "historical_flood_trace_edge_exclusion",
                        path,
                        graph,
                    )
                    record["excluded_baseline_flood_edge_count"] = len(flood_edges)
                    records.append(record)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                pass

    if not records:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")
    routes = gpd.GeoDataFrame(records, crs="EPSG:4326")
    routes = add_flood_metrics(routes)
    routes = add_context_counts(routes)
    return routes


def compare_routes(baselines: gpd.GeoDataFrame, alternatives: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if alternatives.empty:
        return alternatives
    base_index = baselines.set_index("od_id")
    rows = []
    for _, route in alternatives.iterrows():
        base = base_index.loc[route["od_id"]]
        distance_delta = float(route["route_length_m"]) - float(base["route_length_m"])
        overlap_reduction = float(base["flood_overlap_length_m_total"]) - float(route["flood_overlap_length_m_total"])
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
                "historical_flood_trace_avoiding_candidate": bool(overlap_reduction > 0.01),
            }
        )
        rows.append({**record, "geometry": route.geometry})
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def save_outputs(baselines: gpd.GeoDataFrame, alternatives: gpd.GeoDataFrame, destination: dict) -> None:
    combined = pd.concat([baselines, alternatives], ignore_index=True)
    combined.to_file(ROUTES_GEOJSON, driver="GeoJSON")
    table = pd.DataFrame(combined.drop(columns=["geometry"]))
    table.to_csv(ROUTES_CSV, index=False, encoding="utf-8-sig")
    write_json(ROUTES_JSON, table.to_dict("records"))

    exposed = baselines[baselines["flood_overlap_length_m_total"].astype(float) > 0]
    strong = alternatives[
        alternatives.get("historical_flood_trace_avoiding_candidate", pd.Series(dtype=bool)).astype(str).str.lower().eq("true")
    ] if not alternatives.empty else alternatives

    report = {
        "destination": destination,
        "station_names_requested": TARGET_STATION_NAMES,
        "station_line_route_count": int(len(baselines)),
        "baseline_overlap_gt_0_count": int(len(exposed)),
        "baseline_overlap_eq_0_count": int((baselines["flood_overlap_length_m_total"].astype(float) == 0).sum()),
        "alternative_generated_count": int(len(alternatives)),
        "historical_flood_trace_avoiding_candidate_count": int(len(strong)),
        "final_judgement": (
            "센터 목적지 기준으로 baseline 침수흔적 overlap이 있는 역-노선이 있어 회피경로 후보를 생성했다."
            if len(strong)
            else "센터 목적지 기준으로 baseline 침수흔적 overlap이 확인되지 않았거나, 감소 alternative가 확인되지 않았다."
        ),
        "note": "flood_overlap_ratio는 침수확률이 아니라 전체 보행경로 중 과거 침수흔적 geometry와 중첩되는 길이 비율이다.",
        "output_files": [rel(ROUTES_CSV), rel(ROUTES_JSON), rel(ROUTES_GEOJSON), rel(REPORT_JSON)],
    }
    write_json(REPORT_JSON, report)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    graph = ox.load_graphml(GRAPHML_PATH)
    destination = load_destination()
    od_rows = make_station_od_rows(graph, destination)
    baselines = generate_baselines(graph, od_rows)
    alternatives = generate_alternatives_for_exposed(graph, baselines)
    alternatives = compare_routes(baselines, alternatives)
    save_outputs(baselines, alternatives, destination)

    print("## 은평장애인주간보호센터 OSM 보행경로 검증")
    print(f"* 목적지: {destination['facility_name']}")
    print(f"* 주소: {destination['facility_address']}")
    print(f"* 후보 역명: {', '.join(TARGET_STATION_NAMES)}")
    print("")
    for _, row in baselines.sort_values(["station", "station_line"]).iterrows():
        print(
            f"* {row['station']}({row['station_line']}) → 센터: "
            f"baseline={row['route_length_m']}m, overlap={row['flood_overlap_length_m_total']}m, "
            f"flood features={row['flood_feature_count_unique_total']}"
        )
    print("")
    exposed = baselines[baselines["flood_overlap_length_m_total"].astype(float) > 0]
    strong = alternatives[
        alternatives.get("historical_flood_trace_avoiding_candidate", pd.Series(dtype=bool)).astype(str).str.lower().eq("true")
    ] if not alternatives.empty else alternatives
    print(f"* overlap > 0 baseline 수: {len(exposed)}")
    print(f"* 생성한 회피/대체경로 수: {len(alternatives)}")
    print(f"* 실제 감소 후보 수: {len(strong)}")
    if len(strong):
        for _, row in strong.sort_values(["flood_overlap_reduction_m", "distance_delta_m"], ascending=[False, True]).iterrows():
            print(
                f"  - {row['station']}({row['station_line']}): baseline overlap "
                f"{row['baseline_flood_overlap_length_m']}m → {row['alternative_flood_overlap_length_m']}m, "
                f"+{row['distance_delta_m']}m"
            )
    print("")
    print("Saved:")
    print(rel(ROUTES_CSV))
    print(rel(ROUTES_JSON))
    print(rel(ROUTES_GEOJSON))
    print(rel(REPORT_JSON))


if __name__ == "__main__":
    main()
