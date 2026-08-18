from __future__ import annotations

import json
import sys
from itertools import islice
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
from shapely.geometry import Point

SCRIPTS_ROOT = Path(__file__).resolve().parent
if SCRIPTS_ROOT.name != "scripts":
    SCRIPTS_ROOT = SCRIPTS_ROOT.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from build_osm_walking_routing import (
    ACCESSIBILITY_FILE,
    GRAPHML_PATH,
    METRIC_CRS,
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


OUTPUT_DIR = PROJECT_ROOT / "data/processed/final_mvp/end_to_end_validation"
FIRST_MILE_JSON = OUTPUT_DIR / "e2e_first_mile.json"
FIRST_MILE_GEOJSON = OUTPUT_DIR / "e2e_first_mile.geojson"
SUBWAY_SEGMENT_JSON = OUTPUT_DIR / "e2e_subway_segment.json"
LAST_MILE_JSON = OUTPUT_DIR / "e2e_last_mile_validation.json"
FULL_JOURNEY_JSON = OUTPUT_DIR / "e2e_full_journey.json"
FULL_JOURNEY_GEOJSON = OUTPUT_DIR / "e2e_full_journey.geojson"
DIRECT_WALK_JSON = OUTPUT_DIR / "e2e_direct_walk_reference.json"
REPORT_JSON = OUTPUT_DIR / "e2e_validation_report.json"

ORIGIN_NAME = "은평구청"
ORIGIN_ADDRESS = "서울특별시 은평구 은평로 195"
FIRST_STATION = "녹번"
FIRST_STATION_LINE = "3호선"
TRANSFER_STATION = "불광"
TRANSFER_STATION_LINE = "3호선"
DESTINATION_NAME = "불광제1동주민센터"
DESTINATION_ADDRESS = "서울특별시 은평구 진흥로15길 10"

ROUTE_CACHE = PROJECT_ROOT / "data/raw/od_candidates/route_api_cache.json"
YEOKCHON_VALIDATION = PROJECT_ROOT / "data/processed/final_mvp/yeokchon_validation"


def load_station(station_name: str, line_name: str) -> dict:
    master = read_csv_korean(STATION_MASTER_FILE)
    master["line_name"] = master["호선"].map(canonical_line)
    row = master[(master["역사명"].eq(station_name)) & (master["line_name"].eq(line_name))]
    if row.empty:
        raise RuntimeError(f"Station not found: {station_name} {line_name}")
    row = row.iloc[0]
    return {
        "station": station_name,
        "line": line_name,
        "station_code": str(row["역사_ID"]),
        "lon": float(row["경도"]),
        "lat": float(row["위도"]),
    }


def load_origin() -> dict:
    support = flatten_actual_geometry(gpd.read_file(SUPPORT_POINTS_FILE))
    points = support[support.geometry.geom_type == "Point"].copy()
    name_match = points[
        points.get("name", "").fillna("").eq(ORIGIN_NAME)
        | points.get("CONTENTS_NAME", "").fillna("").eq(ORIGIN_NAME)
    ]
    address_match = points[
        points.get("address_new", "").fillna("").eq(ORIGIN_ADDRESS)
        | points.get("ADDR_NEW", "").fillna("").eq(ORIGIN_ADDRESS)
    ].copy()

    if not name_match.empty:
        row = name_match.iloc[0]
        verified_by = "facility_name"
    elif not address_match.empty:
        preferred = address_match[
            address_match.get("name", "").fillna("").str.contains("보건소|주민센터", na=False)
            | address_match.get("CONTENTS_NAME", "").fillna("").str.contains("보건소|주민센터", na=False)
        ]
        row = (preferred if not preferred.empty else address_match).iloc[0]
        verified_by = "address_existing_project_point"
    else:
        return {
            "name": ORIGIN_NAME,
            "address": ORIGIN_ADDRESS,
            "origin_coordinate_unverified": True,
            "verification_note": "프로젝트 기존 공간데이터에서 은평구청명 또는 동일 주소 좌표를 찾지 못했다.",
        }

    return {
        "name": ORIGIN_NAME,
        "address": ORIGIN_ADDRESS,
        "source_record_name": str(row.get("name", row.get("CONTENTS_NAME", ""))),
        "source_id": str(row.get("source_id", "")),
        "lon": float(row.geometry.x),
        "lat": float(row.geometry.y),
        "origin_coordinate_unverified": False,
        "verified_by": verified_by,
        "verification_note": "시설명 exact match는 없을 수 있으나 동일 주소의 기존 point 데이터를 사용했다.",
    }


def load_destination() -> dict:
    support = flatten_actual_geometry(gpd.read_file(SUPPORT_POINTS_FILE))
    points = support[support.geometry.geom_type == "Point"].copy()
    name_rows = points[
        points.get("name", "").fillna("").eq(DESTINATION_NAME)
        | points.get("CONTENTS_NAME", "").fillna("").eq(DESTINATION_NAME)
    ]
    address_rows = points[
        points.get("address_new", "").fillna("").eq(DESTINATION_ADDRESS)
        | points.get("ADDR_NEW", "").fillna("").eq(DESTINATION_ADDRESS)
    ]
    rows = name_rows if not name_rows.empty else address_rows
    if rows.empty:
        raise RuntimeError(f"Destination not found in project data: {DESTINATION_NAME}")
    row = rows.iloc[0]
    return {
        "name": DESTINATION_NAME,
        "address": DESTINATION_ADDRESS,
        "source_record_name": str(row.get("name", row.get("CONTENTS_NAME", ""))),
        "source_id": str(row.get("source_id", "")),
        "lon": float(row.geometry.x),
        "lat": float(row.geometry.y),
    }


def access_summary(station: str, line: str) -> dict:
    if not ACCESSIBILITY_FILE.exists():
        return {"processed_exists": False, "elevator_exists": None, "wheelchair_lift_exists": None, "rows": []}
    df = pd.read_csv(ACCESSIBILITY_FILE, encoding="utf-8")
    rows = df[(df["station_name"].eq(station)) & (df["line_name"].eq(line))].copy()
    if rows.empty:
        return {"processed_exists": False, "elevator_exists": None, "wheelchair_lift_exists": None, "rows": []}
    facility_types = rows["facility_type"].fillna("").astype(str)
    return {
        "processed_exists": True,
        "elevator_exists": bool(facility_types.eq("elevator").any()),
        "wheelchair_lift_exists": bool(facility_types.eq("wheelchair_lift").any()),
        "facility_count": int(len(rows)),
        "oprtngSitu_raw_values": sorted({str(v) for v in rows.get("operation_status", pd.Series(dtype=str)).dropna()}),
        "rows": rows[
            [
                "station_name",
                "station_code",
                "line_name",
                "facility_type",
                "facility_label",
                "nearby_entrance_no",
                "location",
                "operation_status",
            ]
        ].to_dict("records"),
        "note": "시설 존재 여부만 기록하며 operation_status/oprtngSitu는 이용 가능 여부로 해석하지 않는다.",
    }


def route_between_points(
    graph: nx.MultiDiGraph,
    route_id: str,
    start: dict,
    end: dict,
    route_type: str = "baseline",
) -> gpd.GeoDataFrame:
    start_node, start_snap = ox.distance.nearest_nodes(graph, start["lon"], start["lat"], return_dist=True)
    end_node, end_snap = ox.distance.nearest_nodes(graph, end["lon"], end["lat"], return_dist=True)
    graph_d = ox.convert.to_digraph(graph, weight="length")
    path = nx.shortest_path(graph_d, int(start_node), int(end_node), weight="length")
    tags = edge_tag_counts(graph, path)
    row = {
        "route_id": route_id,
        "route_type": route_type,
        "start_name": start["name"],
        "start_lon": start["lon"],
        "start_lat": start["lat"],
        "end_name": end["name"],
        "end_lon": end["lon"],
        "end_lat": end["lat"],
        "start_node": int(start_node),
        "end_node": int(end_node),
        "start_snap_distance_m": float(start_snap),
        "end_snap_distance_m": float(end_snap),
        "routing_success": True,
        "node_sequence": json.dumps([int(node) for node in path], ensure_ascii=False),
        "edge_sequence": json.dumps([[int(u), int(v)] for u, v in zip(path[:-1], path[1:])], ensure_ascii=False),
        "node_count": len(path),
        "edge_count": max(0, len(path) - 1),
        "route_length_m": round(route_length(graph, path), 3),
        "steps_count": tags["steps_count"],
        "geometry": route_linestring(graph, path),
    }
    routes = gpd.GeoDataFrame([row], crs="EPSG:4326")
    routes = add_flood_metrics(routes)
    routes = add_context_counts(routes)
    return routes


def route_record(gdf: gpd.GeoDataFrame) -> dict:
    if gdf.empty:
        return {"routing_success": False}
    row = gdf.iloc[0]
    return {
        key: row[key]
        for key in gdf.columns
        if key != "geometry" and not isinstance(row[key], (bytes, bytearray))
    }


def load_subway_segment() -> dict:
    if not ROUTE_CACHE.exists():
        return {"api_success": False, "source": rel(ROUTE_CACHE), "reason": "route cache not found"}
    data = json.loads(ROUTE_CACHE.read_text(encoding="utf-8"))
    cached = data.get("녹번|불광|")
    if not cached:
        return {"api_success": False, "source": rel(ROUTE_CACHE), "reason": "녹번|불광| cache not found"}
    payload = cached.get("payload", {})
    body = payload.get("body", {})
    paths = body.get("paths") or []
    if isinstance(paths, dict):
        paths = [paths]
    station_sequence = []
    line_sequence = []
    segments = []
    for idx, path in enumerate(paths):
        dep = path.get("dptreStn", {})
        arr = path.get("arvlStn", {})
        if idx == 0:
            station_sequence.append(dep.get("stnNm"))
        station_sequence.append(arr.get("stnNm"))
        line = dep.get("lineNm")
        if line and line not in line_sequence:
            line_sequence.append(line)
        segments.append(
            {
                "departure_station": dep.get("stnNm"),
                "departure_line": dep.get("lineNm"),
                "arrival_station": arr.get("stnNm"),
                "arrival_line": arr.get("lineNm"),
                "distance_raw": path.get("stnSctnDstc"),
                "reqHr_raw": path.get("reqHr"),
                "transfer_yn": path.get("trsitYn"),
            }
        )
    return {
        "api_success": bool(cached.get("ok")),
        "http_status": cached.get("http_status"),
        "resultCode": cached.get("result_code"),
        "resultMsg": cached.get("result_msg"),
        "source": rel(ROUTE_CACHE),
        "origin": {"station": FIRST_STATION, "line": FIRST_STATION_LINE},
        "destination": {"station": TRANSFER_STATION, "line": TRANSFER_STATION_LINE},
        "search_type": body.get("searchType"),
        "totalDstc": body.get("totalDstc"),
        "totalReqHr_raw": body.get("totalReqHr"),
        "transfer_count": body.get("trsitNmtm"),
        "transfer_stations": body.get("trfstnNms"),
        "station_sequence": station_sequence,
        "line_sequence": line_sequence,
        "segments": segments,
    }


def load_last_mile_from_existing() -> dict:
    baseline_csv = YEOKCHON_VALIDATION / "yeokchon_baseline_routes.csv"
    alternatives_csv = YEOKCHON_VALIDATION / "yeokchon_alternative_routes.csv"
    if not baseline_csv.exists() or not alternatives_csv.exists():
        return {"validated": False, "reason": "existing last-mile validation files not found"}
    baselines = pd.read_csv(baseline_csv, encoding="utf-8-sig")
    alternatives = pd.read_csv(alternatives_csv, encoding="utf-8-sig")
    base = baselines[
        baselines["station"].eq(TRANSFER_STATION)
        & baselines["station_line"].eq(TRANSFER_STATION_LINE)
        & baselines["facility_name"].eq(DESTINATION_NAME)
    ]
    alts = alternatives[
        alternatives["station"].eq(TRANSFER_STATION)
        & alternatives["station_line"].eq(TRANSFER_STATION_LINE)
        & alternatives["facility_name"].eq(DESTINATION_NAME)
        & alternatives["historical_flood_trace_avoiding_candidate"].astype(str).str.lower().eq("true")
    ].copy()
    if base.empty or alts.empty:
        return {"validated": False, "reason": "matching baseline or reducing alternative not found"}
    alt = alts.sort_values(["distance_delta_m", "hausdorff_distance_m"], ascending=[True, False]).iloc[0]
    return {
        "validated": True,
        "source_files": [rel(baseline_csv), rel(alternatives_csv)],
        "origin": {"station": TRANSFER_STATION, "line": TRANSFER_STATION_LINE},
        "destination": {"name": DESTINATION_NAME, "address": DESTINATION_ADDRESS},
        "baseline_distance_m": float(alt["baseline_length_m"]),
        "alternative_distance_m": float(alt["alternative_length_m"]),
        "extra_distance_m": float(alt["distance_delta_m"]),
        "baseline_historical_flood_overlap_m": float(alt["baseline_flood_overlap_length_m"]),
        "alternative_historical_flood_overlap_m": float(alt["alternative_flood_overlap_length_m"]),
        "overlap_reduction_m": float(alt["flood_overlap_reduction_m"]),
        "hausdorff_distance_m": float(alt["hausdorff_distance_m"]),
        "method": alt.get("method"),
        "note": "기존 산출물 값을 재확인했으며 total/overlap 값은 새로 보정하지 않았다.",
    }


def compute_last_mile_fallback(graph: nx.MultiDiGraph, bulgwang: dict, destination: dict) -> dict:
    station_node, station_snap = ox.distance.nearest_nodes(graph, bulgwang["lon"], bulgwang["lat"], return_dist=True)
    facility_node, facility_snap = ox.distance.nearest_nodes(
        graph, destination["lon"], destination["lat"], return_dist=True
    )
    od = pd.DataFrame(
        [
            {
                "od_id": "e2e_bulgwang_3_to_bulgwang1dong_center",
                "station": TRANSFER_STATION,
                "station_line": TRANSFER_STATION_LINE,
                "station_lon": bulgwang["lon"],
                "station_lat": bulgwang["lat"],
                "facility_source": "pedestrian_support_point",
                "facility_id": destination["source_id"],
                "facility_name": DESTINATION_NAME,
                "facility_type": "공공시설/주민센터",
                "facility_address": DESTINATION_ADDRESS,
                "facility_lon": destination["lon"],
                "facility_lat": destination["lat"],
                "straight_line_distance_m": None,
                "station_node": int(station_node),
                "facility_node": int(facility_node),
                "station_snap_distance_m": float(station_snap),
                "facility_snap_distance_m": float(facility_snap),
                "snap_status": "PASS",
                "distance_filter_note": "fixed_e2e_last_mile",
            }
        ]
    )
    baseline = build_fixed_od_baseline(graph, od)
    compared = compare_fixed_od_alternatives(graph, baseline, build_fixed_od_alternatives(graph, baseline))
    good = compared[compared["historical_flood_trace_avoiding_candidate"].eq(True)].copy()
    if baseline.empty or good.empty:
        return {
            "validated": False,
            "reason": "fixed last-mile OSM fallback did not find a reducing alternative",
            "source_files": [],
        }
    best = good.sort_values(["distance_delta_m", "hausdorff_distance_m"], ascending=[True, False]).iloc[0]
    return {
        "validated": True,
        "source_files": ["computed_in_scripts\\validate_final_e2e_mvp.py"],
        "origin": {"station": TRANSFER_STATION, "line": TRANSFER_STATION_LINE},
        "destination": {"name": DESTINATION_NAME, "address": DESTINATION_ADDRESS},
        "baseline_distance_m": float(best["baseline_length_m"]),
        "alternative_distance_m": float(best["alternative_length_m"]),
        "extra_distance_m": float(best["distance_delta_m"]),
        "baseline_historical_flood_overlap_m": float(best["baseline_flood_overlap_length_m"]),
        "alternative_historical_flood_overlap_m": float(best["alternative_flood_overlap_length_m"]),
        "overlap_reduction_m": float(best["flood_overlap_reduction_m"]),
        "hausdorff_distance_m": float(best["hausdorff_distance_m"]),
        "method": best.get("method"),
        "note": "기존 저장 파일에서 고정 OD를 찾지 못해 동일 로컬 OSM graph와 flood data로 고정 OD만 재계산했다.",
    }


def build_fixed_od_baseline(graph: nx.MultiDiGraph, od: pd.DataFrame) -> gpd.GeoDataFrame:
    graph_d = ox.convert.to_digraph(graph, weight="length")
    rows = []
    for _, row in od.iterrows():
        path = nx.shortest_path(graph_d, int(row["station_node"]), int(row["facility_node"]), weight="length")
        tags = edge_tag_counts(graph, path)
        rows.append(
            {
                **row.to_dict(),
                "route_id": f"{row['od_id']}_baseline",
                "route_type": "baseline",
                "method": "shortest_path_by_length",
                "node_sequence": json.dumps([int(node) for node in path], ensure_ascii=False),
                "edge_sequence": json.dumps([[int(u), int(v)] for u, v in zip(path[:-1], path[1:])], ensure_ascii=False),
                "node_count": len(path),
                "edge_count": max(0, len(path) - 1),
                "route_length_m": round(route_length(graph, path), 3),
                "steps_count": tags["steps_count"],
                "geometry": route_linestring(graph, path),
            }
        )
    routes = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    routes = add_flood_metrics(routes)
    routes = add_context_counts(routes)
    return routes


def parse_nodes(value: object) -> list[int]:
    return [int(node) for node in json.loads(str(value))]


def build_fixed_od_alternatives(graph: nx.MultiDiGraph, baselines: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
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
                    12,
                )
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            paths = []
        alt_count = 0
        for path in paths[1:]:
            length = route_length(graph, path)
            if length > float(baseline["route_length_m"]) * 2.0:
                continue
            if any(node_overlap_ratio(path, existing) >= 0.9 for existing in accepted):
                continue
            alt_count += 1
            accepted.append(path)
            records.append(make_fixed_od_route_record(baseline.drop(labels=["geometry"]), f"k_shortest_alternative_{alt_count}", "k_shortest", path, graph))
            if alt_count >= 5:
                break

        flood_edges = get_fixed_baseline_flood_edges(baseline, graph)
        if flood_edges:
            avoid_graph = graph_d.copy()
            avoid_graph.remove_edges_from(list(flood_edges))
            try:
                path = nx.shortest_path(
                    avoid_graph, int(baseline["station_node"]), int(baseline["facility_node"]), weight="length"
                )
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                path = None
            if path and node_overlap_ratio(path, baseline_nodes) < 0.9:
                record = make_fixed_od_route_record(
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


def make_fixed_od_route_record(
    row: pd.Series, route_type: str, method: str, path: list[int], graph: nx.MultiDiGraph
) -> dict:
    tags = edge_tag_counts(graph, path)
    return {
        **row.to_dict(),
        "route_id": f"{row['od_id']}_{route_type}",
        "route_type": route_type,
        "method": method,
        "node_sequence": json.dumps([int(node) for node in path], ensure_ascii=False),
        "edge_sequence": json.dumps([[int(u), int(v)] for u, v in zip(path[:-1], path[1:])], ensure_ascii=False),
        "node_count": len(path),
        "edge_count": max(0, len(path) - 1),
        "route_length_m": round(route_length(graph, path), 3),
        "steps_count": tags["steps_count"],
        "geometry": route_linestring(graph, path),
    }


def get_fixed_baseline_flood_edges(row: pd.Series, graph: nx.MultiDiGraph) -> set[tuple[int, int]]:
    nodes = parse_nodes(row["node_sequence"])
    edge_routes = [
        {
            "u": int(u),
            "v": int(v),
            "route_length_m": route_length(graph, [int(u), int(v)]),
            "geometry": route_linestring(graph, [int(u), int(v)]),
        }
        for u, v in zip(nodes[:-1], nodes[1:])
    ]
    edge_gdf = gpd.GeoDataFrame(edge_routes, crs="EPSG:4326")
    if edge_gdf.empty:
        return set()
    edge_gdf = add_flood_metrics(edge_gdf)
    flooded = edge_gdf[edge_gdf["flood_overlap_length_m_total"] > 0]
    return {(int(r["u"]), int(r["v"])) for _, r in flooded.iterrows()}


def compare_fixed_od_alternatives(
    graph: nx.MultiDiGraph, baselines: gpd.GeoDataFrame, alternatives: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
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
                "baseline_flood_overlap_length_m": round(float(base["flood_overlap_length_m_total"]), 3),
                "alternative_flood_overlap_length_m": round(float(route["flood_overlap_length_m_total"]), 3),
                "flood_overlap_reduction_m": round(overlap_reduction, 3),
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


def full_journey_geojson(first_mile: gpd.GeoDataFrame, graph: nx.MultiDiGraph, last_mile: dict) -> None:
    features = []
    if not first_mile.empty:
        row = first_mile.iloc[0]
        features.append(
            {
                "segment_id": "segment_1_first_mile",
                "mode": "walk",
                "route_role": "first_mile",
                "start": ORIGIN_NAME,
                "end": f"{FIRST_STATION}역 {FIRST_STATION_LINE}",
                "route_length_m": float(row["route_length_m"]),
                "flood_overlap_length_m": float(row["flood_overlap_length_m_total"]),
                "geometry": row.geometry,
            }
        )
    # Reconstruct last-mile geometries from existing CSV node sequences to avoid inventing geometry.
    base_csv = YEOKCHON_VALIDATION / "yeokchon_baseline_routes.csv"
    alt_csv = YEOKCHON_VALIDATION / "yeokchon_alternative_routes.csv"
    if base_csv.exists() and alt_csv.exists():
        base = pd.read_csv(base_csv, encoding="utf-8-sig")
        alt = pd.read_csv(alt_csv, encoding="utf-8-sig")
        base = base[
            base["station"].eq(TRANSFER_STATION)
            & base["station_line"].eq(TRANSFER_STATION_LINE)
            & base["facility_name"].eq(DESTINATION_NAME)
        ]
        alt = alt[
            alt["station"].eq(TRANSFER_STATION)
            & alt["station_line"].eq(TRANSFER_STATION_LINE)
            & alt["facility_name"].eq(DESTINATION_NAME)
            & alt["historical_flood_trace_avoiding_candidate"].astype(str).str.lower().eq("true")
        ].copy()
        if not base.empty:
            row = base.iloc[0]
            nodes = [int(node) for node in json.loads(row["node_sequence"])]
            features.append(
                {
                    "segment_id": "segment_3_last_mile_baseline",
                    "mode": "walk",
                    "route_role": "last_mile_baseline",
                    "start": f"{TRANSFER_STATION}역 {TRANSFER_STATION_LINE}",
                    "end": DESTINATION_NAME,
                    "route_length_m": float(row["route_length_m"]),
                    "flood_overlap_length_m": float(row["flood_overlap_length_m_total"]),
                    "geometry": route_linestring(graph, nodes),
                }
            )
        if not alt.empty:
            row = alt.sort_values(["distance_delta_m", "hausdorff_distance_m"], ascending=[True, False]).iloc[0]
            nodes = [int(node) for node in json.loads(row["node_sequence"])]
            features.append(
                {
                    "segment_id": "segment_3_last_mile_alternative",
                    "mode": "walk",
                    "route_role": "last_mile_alternative",
                    "start": f"{TRANSFER_STATION}역 {TRANSFER_STATION_LINE}",
                    "end": DESTINATION_NAME,
                    "route_length_m": float(row["alternative_length_m"]),
                    "flood_overlap_length_m": float(row["alternative_flood_overlap_length_m"]),
                    "geometry": route_linestring(graph, nodes),
                }
            )
    gdf = gpd.GeoDataFrame(features, crs="EPSG:4326")
    gdf.to_file(FULL_JOURNEY_GEOJSON, driver="GeoJSON")


def clean_jsonable(value):
    if isinstance(value, dict):
        return {key: clean_jsonable(val) for key, val in value.items()}
    if isinstance(value, list):
        return [clean_jsonable(item) for item in value]
    if not isinstance(value, (dict, list, tuple)):
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not GRAPHML_PATH.exists():
        raise RuntimeError(f"OSM graph not found: {rel(GRAPHML_PATH)}")
    graph = ox.load_graphml(GRAPHML_PATH)

    origin = load_origin()
    if origin.get("origin_coordinate_unverified"):
        write_json(REPORT_JSON, {"case": "CASE C", "origin": origin})
        print("1. 은평구청 좌표 확인 여부")
        print("- False")
        print("10. 최종 CASE A / B / C")
        print("CASE C")
        print("전체 MVP 시나리오 동결 보류 — 원인 확인 필요")
        return

    nokbeon = load_station(FIRST_STATION, FIRST_STATION_LINE)
    bulgwang = load_station(TRANSFER_STATION, TRANSFER_STATION_LINE)
    destination = load_destination()

    first_mile = route_between_points(
        graph,
        "e2e_first_mile_eunpyeong_office_to_nokbeon_3",
        origin,
        {"name": f"{FIRST_STATION}역 {FIRST_STATION_LINE}", "lon": nokbeon["lon"], "lat": nokbeon["lat"]},
    )
    first_mile.to_file(FIRST_MILE_GEOJSON, driver="GeoJSON")
    write_json(FIRST_MILE_JSON, clean_jsonable({"origin": origin, "station": nokbeon, "route": route_record(first_mile)}))

    direct_walk = route_between_points(
        graph,
        "e2e_reference_direct_walk_eunpyeong_office_to_bulgwang1dong_center",
        origin,
        destination,
        "direct_walk_reference",
    )
    write_json(DIRECT_WALK_JSON, clean_jsonable({"origin": origin, "destination": destination, "route": route_record(direct_walk)}))

    subway = load_subway_segment()
    write_json(SUBWAY_SEGMENT_JSON, clean_jsonable(subway))

    access_nokbeon = access_summary(FIRST_STATION, FIRST_STATION_LINE)
    access_bulgwang = access_summary(TRANSFER_STATION, TRANSFER_STATION_LINE)

    last_mile = load_last_mile_from_existing()
    if not last_mile.get("validated"):
        last_mile = compute_last_mile_fallback(graph, bulgwang, destination)
    write_json(LAST_MILE_JSON, clean_jsonable(last_mile))
    full_journey_geojson(first_mile, graph, last_mile)

    first_len = float(first_mile.iloc[0]["route_length_m"])
    last_len = float(last_mile["alternative_distance_m"]) if last_mile.get("validated") else None
    direct_len = float(direct_walk.iloc[0]["route_length_m"])
    total_walking_with_subway = first_len + (last_len or 0.0)
    walking_reduction = direct_len - total_walking_with_subway if last_len is not None else None
    naturalness_warning = bool(walking_reduction is not None and walking_reduction < 50)
    naturalness = (
        "지하철 이용으로 보행거리가 충분히 줄지는 않아 발표 시 이유 설명이 필요하다."
        if naturalness_warning
        else "보행-지하철-보행 구조가 데이터상 연결되며 교통약자 조건을 설명하기에 충분하다."
    )

    segments = [
        {
            "segment_id": "segment_1",
            "mode": "walk",
            "start": ORIGIN_NAME,
            "end": f"{FIRST_STATION}역 {FIRST_STATION_LINE}",
            "distance_m": first_len,
            "flood_overlap_length_m": float(first_mile.iloc[0]["flood_overlap_length_m_total"]),
            "data_source": rel(GRAPHML_PATH),
            "validation_status": "PASS",
        },
        {
            "segment_id": "segment_2",
            "mode": "subway",
            "start": f"{FIRST_STATION}역 {FIRST_STATION_LINE}",
            "end": f"{TRANSFER_STATION}역 {TRANSFER_STATION_LINE}",
            "distance_raw": subway.get("totalDstc"),
            "totalReqHr_raw": subway.get("totalReqHr_raw"),
            "data_source": subway.get("source"),
            "validation_status": "PASS" if subway.get("api_success") else "WARNING",
            "geometry_note": "프로젝트에 신뢰 가능한 지하철 선형 geometry가 없어 station sequence만 저장했다.",
        },
        {
            "segment_id": "segment_3",
            "mode": "walk",
            "start": f"{TRANSFER_STATION}역 {TRANSFER_STATION_LINE}",
            "end": DESTINATION_NAME,
            "baseline_distance_m": last_mile.get("baseline_distance_m"),
            "alternative_distance_m": last_mile.get("alternative_distance_m"),
            "baseline_flood_overlap_length_m": last_mile.get("baseline_historical_flood_overlap_m"),
            "alternative_flood_overlap_length_m": last_mile.get("alternative_historical_flood_overlap_m"),
            "data_source": last_mile.get("source_files"),
            "validation_status": "PASS" if last_mile.get("validated") else "FAIL",
        },
    ]
    full = {
        "scenario": {
            "origin": origin,
            "first_station": nokbeon,
            "subway_destination_station": bulgwang,
            "final_destination": destination,
        },
        "segments": segments,
        "total_walking_distance_with_subway_m": round(total_walking_with_subway, 3) if last_len else None,
        "direct_walking_reference_distance_m": direct_len,
        "walking_distance_reduction_reference_m": round(walking_reduction, 3) if walking_reduction is not None else None,
        "naturalness_evaluation": naturalness,
    }
    write_json(FULL_JOURNEY_JSON, clean_jsonable(full))

    case = "CASE B" if naturalness_warning else "CASE A"
    if not first_mile.iloc[0]["routing_success"] or not subway.get("api_success") or not last_mile.get("validated"):
        case = "CASE C"
    final_message = {
        "CASE A": "전체 MVP 시나리오 동결 추천",
        "CASE B": "전체 MVP 시나리오 동결 추천 - 발표 시 지하철 이용 이유 설명 필요",
        "CASE C": "전체 MVP 시나리오 동결 보류 - 원인 확인 필요",
    }[case]
    report = {
        "origin_coordinate_verified": not origin.get("origin_coordinate_unverified"),
        "origin": origin,
        "first_mile": route_record(first_mile),
        "nokbeon_accessibility": access_nokbeon,
        "subway_segment": subway,
        "bulgwang_accessibility": access_bulgwang,
        "last_mile": last_mile,
        "direct_walk_reference": route_record(direct_walk),
        "naturalness_evaluation": naturalness,
        "case": case,
        "final_message": final_message,
        "note": "flood_overlap_ratio는 침수확률이 아니라 전체 경로 길이 중 2022~2025 과거 침수흔적 geometry와 공간적으로 중첩되는 길이 비율이다.",
        "output_files": [
            rel(FIRST_MILE_JSON),
            rel(FIRST_MILE_GEOJSON),
            rel(SUBWAY_SEGMENT_JSON),
            rel(LAST_MILE_JSON),
            rel(FULL_JOURNEY_JSON),
            rel(FULL_JOURNEY_GEOJSON),
            rel(DIRECT_WALK_JSON),
            rel(REPORT_JSON),
        ],
    }
    write_json(REPORT_JSON, clean_jsonable(report))

    print("1. 은평구청 좌표 확인 여부")
    print(f"- {not origin.get('origin_coordinate_unverified')} ({origin['lon']}, {origin['lat']}, source: {origin['source_record_name']})")
    print("2. 은평구청 → 녹번역 first-mile 거리 및 routing 성공 여부")
    print(f"- {first_len}m, routing_success: True")
    print("3. first-mile historical flood overlap")
    print(
        f"- {float(first_mile.iloc[0]['flood_overlap_length_m_total'])}m, "
        f"features: {int(first_mile.iloc[0]['flood_feature_count_unique_total'])}"
    )
    print("4. 녹번역 접근성 시설 존재정보")
    print(
        f"- elevator_exists: {access_nokbeon['elevator_exists']}, "
        f"wheelchair_lift_exists: {access_nokbeon['wheelchair_lift_exists']}"
    )
    print("5. 녹번역 → 불광역 3호선 subway 검증 결과")
    print(
        f"- api_success: {subway.get('api_success')}, result: {subway.get('resultCode')}/{subway.get('resultMsg')}, "
        f"totalDstc: {subway.get('totalDstc')}, totalReqHr_raw: {subway.get('totalReqHr_raw')}, "
        f"transfer_count: {subway.get('transfer_count')}, sequence: {subway.get('station_sequence')}"
    )
    print("6. 불광역 접근성 시설 존재정보")
    print(
        f"- elevator_exists: {access_bulgwang['elevator_exists']}, "
        f"wheelchair_lift_exists: {access_bulgwang['wheelchair_lift_exists']}"
    )
    print("7. 불광역 → 불광제1동주민센터 기존 last-mile 재확인 결과")
    print(
        f"- baseline: {last_mile.get('baseline_distance_m')}m, alternative: {last_mile.get('alternative_distance_m')}m, "
        f"extra: +{last_mile.get('extra_distance_m')}m, overlap: "
        f"{last_mile.get('baseline_historical_flood_overlap_m')}m -> {last_mile.get('alternative_historical_flood_overlap_m')}m, "
        f"Hausdorff: {last_mile.get('hausdorff_distance_m')}m"
    )
    print("8. 은평구청 → 주민센터 직접 보행 참고 거리")
    print(f"- {direct_len}m")
    print("9. 전체 시나리오 자연스러움 평가")
    print(f"- {naturalness}")
    print("10. 최종 CASE A / B / C")
    print(case)
    print(final_message)


if __name__ == "__main__":
    main()
