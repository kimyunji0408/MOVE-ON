from __future__ import annotations

import json
from itertools import islice
from pathlib import Path

import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
from shapely.geometry import LineString, Point, mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_OSM_DIR = PROJECT_ROOT / "data/raw/osm"
OUTPUT_DIR = PROJECT_ROOT / "data/processed/osm_routing"

GRAPHML_PATH = RAW_OSM_DIR / "eunpyeong_walk.graphml"
NODES_GEOJSON = OUTPUT_DIR / "eunpyeong_walk_nodes.geojson"
EDGES_GEOJSON = OUTPUT_DIR / "eunpyeong_walk_edges.geojson"

NETWORK_VALIDATION_JSON = OUTPUT_DIR / "osm_walk_network_validation.json"
TAG_COVERAGE_JSON = OUTPUT_DIR / "osm_accessibility_tag_coverage.json"
OD_CANDIDATES_CSV = OUTPUT_DIR / "station_facility_od_candidates.csv"
ROUTES_CSV = OUTPUT_DIR / "osm_walking_routes.csv"
ROUTES_JSON = OUTPUT_DIR / "osm_walking_routes.json"
TOP_JSON = OUTPUT_DIR / "top_historical_flood_avoiding_candidates.json"
TOP_GEOJSON = OUTPUT_DIR / "top_historical_flood_avoiding_candidates.geojson"
REPORT_JSON = OUTPUT_DIR / "osm_routing_analysis_report.json"

VALIDATION_REPORT = PROJECT_ROOT / "data/processed/mobility/validation_report.json"
FLOOD_FILE = PROJECT_ROOT / "data/processed/flood/seoul_flood_trace_2022_2025.geojson"
MOBILITY_FILE = PROJECT_ROOT / "data/processed/mobility/eunpyeong_mobility_facilities.geojson"
PEDESTRIAN_SAFE_FILE = PROJECT_ROOT / "data/processed/mobility/eunpyeong_pedestrian_safe_routes.geojson"
SUPPORT_POINTS_FILE = PROJECT_ROOT / "data/processed/mobility/eunpyeong_pedestrian_support_points.geojson"
STATION_MASTER_FILE = PROJECT_ROOT / "data/raw/subway/서울시 역사마스터 정보.csv"
STATION_ADDRESS_FILE = PROJECT_ROOT / "data/raw/subway/서울교통공사_역주소 및 전화번호_20250318.csv"
ACCESSIBILITY_FILE = PROJECT_ROOT / "data/processed/accessibility/candidate_station_accessibility.csv"

TARGET_STATIONS = {"녹번", "불광", "독바위", "연신내", "응암"}
YEARS = [2022, 2023, 2024, 2025]
METRIC_CRS = "EPSG:5186"
OD_MIN_DISTANCE_M = 500
OD_MAX_DISTANCE_M = 1500
MAX_ROUTING_OD = 80
MAX_SIMPLE_PATHS_PER_OD = 8
MAX_ALTERNATIVES_PER_OD = 5
MAX_ALT_LENGTH_RATIO = 2.0
MIN_DIFFERENCE_NODE_OVERLAP = 0.9
ROUTE_CONTEXT_BUFFER_M = 30


def rel(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT)).replace("/", "\\")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_csv_korean(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            df = pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
        if any(col in df.columns for col in ("역사명", "역명", "station_name")):
            return df
    raise RuntimeError(f"Could not read CSV with Korean headers: {path}")


def canonical_line(value: object) -> str:
    text = str(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    return f"{int(digits)}호선" if digits else text


def load_stations() -> gpd.GeoDataFrame:
    master = read_csv_korean(STATION_MASTER_FILE)
    addr = read_csv_korean(STATION_ADDRESS_FILE)
    accessibility = pd.read_csv(ACCESSIBILITY_FILE, encoding="utf-8")

    eunpyeong_names = set(
        addr[
            addr["도로명주소"].fillna("").str.contains("은평구")
            | addr["지번주소"].fillna("").str.contains("은평구")
        ]["역명"]
    )
    accessible_names = set(accessibility["station_name"])
    accessible_station_lines = set(zip(accessibility["station_name"], accessibility["line_name"]))

    rows = []
    for _, row in master.iterrows():
        station = str(row["역사명"])
        line = canonical_line(row["호선"])
        if station not in TARGET_STATIONS or station not in eunpyeong_names or station not in accessible_names:
            continue
        if (station, line) not in accessible_station_lines:
            continue
        lon = pd.to_numeric(row["경도"], errors="coerce")
        lat = pd.to_numeric(row["위도"], errors="coerce")
        if pd.isna(lon) or pd.isna(lat):
            continue
        rows.append(
            {
                "station": station,
                "line": line,
                "lon": float(lon),
                "lat": float(lat),
                "geometry": Point(float(lon), float(lat)),
            }
        )
    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    return gdf.drop_duplicates(subset=["station", "line"]).reset_index(drop=True)


def flatten_actual_geometry(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    rows = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None:
            continue
        if geom.geom_type == "GeometryCollection":
            geoms = [g for g in geom.geoms if not g.is_empty]
            if len(geoms) != 1:
                continue
            geom = geoms[0]
        new_row = row.copy()
        new_row.geometry = geom
        rows.append(new_row)
    return gpd.GeoDataFrame(rows, crs=gdf.crs)


def load_facilities() -> gpd.GeoDataFrame:
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
        points["facility_name"] = points.get("name", points.get("CONTENTS_NAME", "")).fillna("")
        points["address"] = points.get("address_new", points.get("ADDR_NEW", "")).fillna("")
        fallback_address = points.get("address_old", points.get("ADDR_OLD", "")).fillna("")
        points.loc[points["address"].eq(""), "address"] = fallback_address
        frames.append(points[["facility_source", "facility_name", "address", "geometry"]])
    return pd.concat(frames, ignore_index=True).pipe(gpd.GeoDataFrame, crs="EPSG:4326")


def get_osm_bbox(stations: gpd.GeoDataFrame, facilities: gpd.GeoDataFrame) -> tuple[float, float, float, float]:
    validation = read_json(VALIDATION_REPORT)
    bbox = validation["eunpyeong_filtering"]["filtered_eunpyeong_bbox"]
    min_lon, min_lat, max_lon, max_lat = map(float, bbox)
    bounds = pd.concat([stations, facilities], ignore_index=True).total_bounds
    min_lon = min(min_lon, float(bounds[0])) - 0.006
    min_lat = min(min_lat, float(bounds[1])) - 0.006
    max_lon = max(max_lon, float(bounds[2])) + 0.006
    max_lat = max(max_lat, float(bounds[3])) + 0.006
    return min_lon, min_lat, max_lon, max_lat


def build_or_load_graph(bbox: tuple[float, float, float, float]) -> tuple[nx.MultiDiGraph, bool]:
    ox.settings.use_cache = True
    ox.settings.log_console = False
    ox.settings.timeout = 180
    ox.settings.useful_tags_way = sorted(
        set(ox.settings.useful_tags_way)
        | {"foot", "sidewalk", "steps", "incline", "wheelchair", "kerb", "surface", "smoothness"}
    )
    ox.settings.useful_tags_node = sorted(set(ox.settings.useful_tags_node) | {"wheelchair", "kerb", "highway"})

    if GRAPHML_PATH.exists():
        return ox.load_graphml(GRAPHML_PATH), False

    left, bottom, right, top = bbox
    graph = ox.graph_from_bbox((left, bottom, right, top), network_type="walk", retain_all=True, simplify=True)
    ox.save_graphml(graph, GRAPHML_PATH)
    return graph, True


def graph_to_geojson(graph: nx.MultiDiGraph) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    nodes, edges = ox.graph_to_gdfs(graph)
    nodes.to_file(NODES_GEOJSON, driver="GeoJSON")
    edges.to_file(EDGES_GEOJSON, driver="GeoJSON")
    return nodes, edges


def validate_graph(graph: nx.MultiDiGraph, edges: gpd.GeoDataFrame, downloaded: bool) -> dict:
    components = list(nx.weakly_connected_components(graph))
    largest = max((len(c) for c in components), default=0)
    edge_lengths = pd.to_numeric(edges.get("length"), errors="coerce")
    bbox = edges.to_crs("EPSG:4326").total_bounds.tolist() if not edges.empty else None
    return {
        "download_success": bool(graph.number_of_nodes() > 0 and graph.number_of_edges() > 0),
        "downloaded_this_run": downloaded,
        "graphml_saved": GRAPHML_PATH.exists(),
        "graphml_path": rel(GRAPHML_PATH),
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "crs": str(graph.graph.get("crs")),
        "bbox": bbox,
        "connected_component_count": len(components),
        "largest_connected_component_ratio": round(largest / graph.number_of_nodes(), 6) if graph.number_of_nodes() else None,
        "edge_length": {
            "min": float(edge_lengths.min()) if edge_lengths.notna().any() else None,
            "median": float(edge_lengths.median()) if edge_lengths.notna().any() else None,
            "max": float(edge_lengths.max()) if edge_lengths.notna().any() else None,
        },
        "geometry_missing_count": int(edges.geometry.isna().sum()),
        "pedestrian_routing_suitable": bool(graph.number_of_nodes() > 0 and graph.number_of_edges() > 0),
        "largest_component_note": "retain_all=True로 전체 graph를 보존했으며, 경로별 connected component 여부를 routing 시 확인한다.",
    }


def tag_coverage(edges: gpd.GeoDataFrame, nodes: gpd.GeoDataFrame) -> dict:
    def summarize(gdf: gpd.GeoDataFrame, tags: list[str]) -> dict:
        result = {}
        total = len(gdf)
        for tag in tags:
            if tag not in gdf.columns:
                result[tag] = {"count": 0, "coverage": 0.0, "examples": []}
                continue
            values = gdf[tag].dropna()
            values = values[values.astype(str).str.len() > 0]
            result[tag] = {
                "count": int(len(values)),
                "coverage": round(len(values) / total, 6) if total else 0.0,
                "examples": sorted({str(v) for v in values.head(30)})[:10],
            }
        return result

    return {
        "edge_tags": summarize(
            edges,
            ["highway", "foot", "sidewalk", "steps", "incline", "wheelchair", "kerb", "surface", "smoothness"],
        ),
        "node_tags": summarize(nodes, ["wheelchair", "kerb", "highway"]),
        "use_in_risk_score": False,
        "note": "Coverage가 충분하거나 의미가 공식 검증되기 전까지 OSM 접근성 tag는 참고 정보로만 보존한다.",
    }


def build_od_candidates(stations: gpd.GeoDataFrame, facilities: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    stations_m = stations.to_crs(METRIC_CRS)
    facilities_m = facilities.to_crs(METRIC_CRS)
    rows = []
    for s_idx, station in stations_m.iterrows():
        for f_idx, facility in facilities_m.iterrows():
            distance = float(station.geometry.distance(facility.geometry))
            if OD_MIN_DISTANCE_M <= distance <= OD_MAX_DISTANCE_M:
                rows.append(
                    {
                        "od_id": f"od_{len(rows) + 1}",
                        "station": stations.loc[s_idx, "station"],
                        "station_line": stations.loc[s_idx, "line"],
                        "station_lon": stations.loc[s_idx, "lon"],
                        "station_lat": stations.loc[s_idx, "lat"],
                        "facility_source": facilities.loc[f_idx, "facility_source"],
                        "facility_name": facilities.loc[f_idx, "facility_name"],
                        "facility_address": facilities.loc[f_idx, "address"],
                        "facility_lon": float(facilities.loc[f_idx].geometry.x),
                        "facility_lat": float(facilities.loc[f_idx].geometry.y),
                        "straight_line_distance_m": round(distance, 3),
                    }
                )
    df = pd.DataFrame(rows).sort_values(
        ["straight_line_distance_m", "station", "facility_name"], ascending=[False, True, True]
    )
    df.to_csv(OD_CANDIDATES_CSV, index=False, encoding="utf-8-sig")
    return gpd.GeoDataFrame(
        df,
        geometry=[Point(xy) for xy in zip(df["facility_lon"], df["facility_lat"])],
        crs="EPSG:4326",
    )


def snap_od_candidates(graph: nx.MultiDiGraph, od: pd.DataFrame) -> pd.DataFrame:
    if od.empty:
        return od
    station_nodes, station_dists = ox.distance.nearest_nodes(
        graph, od["station_lon"].to_numpy(), od["station_lat"].to_numpy(), return_dist=True
    )
    facility_nodes, facility_dists = ox.distance.nearest_nodes(
        graph, od["facility_lon"].to_numpy(), od["facility_lat"].to_numpy(), return_dist=True
    )
    out = od.copy()
    out["station_node"] = station_nodes
    out["facility_node"] = facility_nodes
    out["station_snap_distance_m"] = station_dists
    out["facility_snap_distance_m"] = facility_dists
    out["snap_status"] = out.apply(
        lambda r: "PASS" if r["station_snap_distance_m"] <= 100 and r["facility_snap_distance_m"] <= 100 else "WARNING",
        axis=1,
    )
    out.to_csv(OD_CANDIDATES_CSV, index=False, encoding="utf-8-sig")
    return out


def choose_edge_data(graph: nx.MultiDiGraph, u: int, v: int) -> dict:
    data = graph.get_edge_data(u, v)
    if not data:
        return {}
    return min(data.values(), key=lambda d: float(d.get("length", 0)))


def route_linestring(graph: nx.MultiDiGraph, path: list[int]) -> LineString:
    coords: list[tuple[float, float]] = []
    for u, v in zip(path[:-1], path[1:]):
        edge = choose_edge_data(graph, u, v)
        geom = edge.get("geometry")
        if geom is not None:
            edge_coords = list(geom.coords)
        else:
            edge_coords = [(graph.nodes[u]["x"], graph.nodes[u]["y"]), (graph.nodes[v]["x"], graph.nodes[v]["y"])]
        if coords and coords[-1] == edge_coords[0]:
            coords.extend(edge_coords[1:])
        else:
            coords.extend(edge_coords)
    return LineString(coords)


def route_length(graph: nx.MultiDiGraph, path: list[int]) -> float:
    total = 0.0
    for u, v in zip(path[:-1], path[1:]):
        total += float(choose_edge_data(graph, u, v).get("length", 0.0))
    return total


def node_overlap_ratio(a: list[int], b: list[int]) -> float:
    if not a or not b:
        return 0.0
    return len(set(a) & set(b)) / min(len(set(a)), len(set(b)))


def edge_tag_counts(graph: nx.MultiDiGraph, path: list[int]) -> dict:
    counts = {"steps_count": 0}
    for u, v in zip(path[:-1], path[1:]):
        edge = choose_edge_data(graph, u, v)
        highway = edge.get("highway")
        values = highway if isinstance(highway, list) else [highway]
        if any(str(value) == "steps" for value in values):
            counts["steps_count"] += 1
    return counts


def generate_routes(graph: nx.MultiDiGraph, od: pd.DataFrame) -> gpd.GeoDataFrame:
    graph_d = ox.convert.to_digraph(graph, weight="length")
    route_rows = []
    for _, row in od.head(MAX_ROUTING_OD).iterrows():
        if row["snap_status"] == "WARNING" or row["station_node"] == row["facility_node"]:
            continue
        source = int(row["station_node"])
        target = int(row["facility_node"])
        try:
            generator = nx.shortest_simple_paths(graph_d, source, target, weight="length")
            paths = list(islice(generator, MAX_SIMPLE_PATHS_PER_OD))
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        if not paths:
            continue
        baseline = paths[0]
        baseline_length = route_length(graph, baseline)
        accepted = [(baseline, "baseline")]
        alt_count = 0
        for path in paths[1:]:
            length = route_length(graph, path)
            if baseline_length and length > baseline_length * MAX_ALT_LENGTH_RATIO:
                continue
            if any(node_overlap_ratio(path, existing) >= MIN_DIFFERENCE_NODE_OVERLAP for existing, _ in accepted):
                continue
            alt_count += 1
            accepted.append((path, f"alternative_{alt_count}"))
            if alt_count >= MAX_ALTERNATIVES_PER_OD:
                break
        for path, route_type in accepted:
            tags = edge_tag_counts(graph, path)
            route_rows.append(
                {
                    **row.drop(labels=["geometry"], errors="ignore").to_dict(),
                    "route_id": f"{row['od_id']}_{route_type}",
                    "route_type": route_type,
                    "node_sequence": json.dumps([int(node) for node in path], ensure_ascii=False),
                    "node_count": len(path),
                    "route_length_m": round(route_length(graph, path), 3),
                    "steps_count": tags["steps_count"],
                    "geometry": route_linestring(graph, path),
                }
            )
    return gpd.GeoDataFrame(route_rows, crs="EPSG:4326")


def add_flood_metrics(routes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if routes.empty:
        return routes
    flood = gpd.read_file(FLOOD_FILE)
    flood = flood[flood["ADM_CD"].astype(str).str.startswith("11380")].copy()
    flood["flood_id"] = [f"flood_{i}" for i in range(len(flood))]
    flood["F_YR"] = pd.to_numeric(flood["F_YR"], errors="coerce").astype("Int64")
    flood_m = flood.to_crs(METRIC_CRS)
    routes_m = routes.to_crs(METRIC_CRS)
    output_rows = []
    for idx, route in routes_m.iterrows():
        row = routes.loc[idx].drop(labels=["geometry"]).to_dict()
        geom = route.geometry
        total_overlap = 0.0
        unique_ids = set()
        year_counts = {year: 0 for year in YEARS}
        year_lengths = {year: 0.0 for year in YEARS}
        possible = flood_m[flood_m.intersects(geom)]
        for _, frow in possible.iterrows():
            inter = geom.intersection(frow.geometry)
            overlap = float(inter.length) if not inter.is_empty else 0.0
            if overlap <= 0.01:
                continue
            flood_id = frow["flood_id"]
            year = int(frow["F_YR"]) if pd.notna(frow["F_YR"]) else None
            unique_ids.add(flood_id)
            total_overlap += overlap
            if year in year_counts:
                year_counts[year] += 1
                year_lengths[year] += overlap
        for year in YEARS:
            row[f"flood_feature_count_{year}"] = year_counts[year]
            row[f"flood_overlap_length_m_{year}"] = round(year_lengths[year], 3)
        row["flood_feature_count_unique_total"] = len(unique_ids)
        row["flood_overlap_length_m_total"] = round(total_overlap, 3)
        row["flood_overlap_ratio"] = round(total_overlap / float(route["route_length_m"]), 6) if route["route_length_m"] else None
        output_rows.append({**row, "geometry": routes.loc[idx].geometry})
    return gpd.GeoDataFrame(output_rows, crs="EPSG:4326")


def add_context_counts(routes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if routes.empty:
        return routes
    mobility = flatten_actual_geometry(gpd.read_file(MOBILITY_FILE))
    support = flatten_actual_geometry(gpd.read_file(SUPPORT_POINTS_FILE))
    safe_routes = flatten_actual_geometry(gpd.read_file(PEDESTRIAN_SAFE_FILE))
    mobility_m = mobility[mobility.geometry.geom_type == "Point"].to_crs(METRIC_CRS)
    support_m = support[support.geometry.geom_type == "Point"].to_crs(METRIC_CRS)
    safe_m = safe_routes[safe_routes.geometry.geom_type == "LineString"].to_crs(METRIC_CRS)
    routes_m = routes.to_crs(METRIC_CRS)
    rows = []
    for idx, route in routes_m.iterrows():
        buffered = route.geometry.buffer(ROUTE_CONTEXT_BUFFER_M)
        row = routes.loc[idx].drop(labels=["geometry"]).to_dict()
        row["mobility_facility_count_near_route"] = int(mobility_m.intersects(buffered).sum())
        row["support_point_count_near_route"] = int(support_m.intersects(buffered).sum())
        row["existing_safe_route_overlap_or_proximity_count"] = int(safe_m.intersects(buffered).sum())
        rows.append({**row, "geometry": routes.loc[idx].geometry})
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def compare_routes(routes: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    if routes.empty:
        return routes, pd.DataFrame()
    rows = []
    enriched_rows = []
    for od_id, group in routes.groupby("od_id"):
        baseline = group[group["route_type"] == "baseline"]
        if baseline.empty:
            continue
        base = baseline.iloc[0]
        for idx, route in group.iterrows():
            record = route.drop(labels=["geometry"]).to_dict()
            if route["route_type"] == "baseline":
                record.update(
                    {
                        "baseline_distance": route["route_length_m"],
                        "alternative_distance": None,
                        "distance_delta_m": 0,
                        "distance_delta_ratio": 0,
                        "baseline_flood_overlap_m": route["flood_overlap_length_m_total"],
                        "alternative_flood_overlap_m": None,
                        "flood_overlap_reduction_m": 0,
                        "flood_overlap_reduction_ratio": 0,
                        "historical_flood_trace_avoiding_candidate": False,
                    }
                )
            else:
                distance_delta = float(route["route_length_m"]) - float(base["route_length_m"])
                flood_reduction = float(base["flood_overlap_length_m_total"]) - float(route["flood_overlap_length_m_total"])
                record.update(
                    {
                        "baseline_distance": base["route_length_m"],
                        "alternative_distance": route["route_length_m"],
                        "distance_delta_m": round(distance_delta, 3),
                        "distance_delta_ratio": round(distance_delta / float(base["route_length_m"]), 6)
                        if base["route_length_m"]
                        else None,
                        "baseline_flood_overlap_m": base["flood_overlap_length_m_total"],
                        "alternative_flood_overlap_m": route["flood_overlap_length_m_total"],
                        "flood_overlap_reduction_m": round(flood_reduction, 3),
                        "flood_overlap_reduction_ratio": round(flood_reduction / float(base["flood_overlap_length_m_total"]), 6)
                        if base["flood_overlap_length_m_total"]
                        else None,
                        "historical_flood_trace_avoiding_candidate": bool(
                            distance_delta >= -0.01 and flood_reduction > 0.01
                        ),
                    }
                )
            rows.append(record)
            enriched_rows.append({**record, "geometry": route.geometry})
    return gpd.GeoDataFrame(enriched_rows, crs="EPSG:4326"), pd.DataFrame(rows)


def save_routes(routes: gpd.GeoDataFrame, route_table: pd.DataFrame) -> None:
    if routes.empty:
        pd.DataFrame().to_csv(ROUTES_CSV, index=False, encoding="utf-8-sig")
        write_json(ROUTES_JSON, [])
        return
    csv_df = route_table.drop(columns=["node_sequence"], errors="ignore")
    csv_df.to_csv(ROUTES_CSV, index=False, encoding="utf-8-sig")
    records = []
    for _, row in route_table.iterrows():
        record = row.to_dict()
        records.append(record)
    write_json(ROUTES_JSON, records)


def save_top(routes: gpd.GeoDataFrame) -> list[dict]:
    candidates = routes[routes["historical_flood_trace_avoiding_candidate"] == True].copy()  # noqa: E712
    if candidates.empty:
        write_json(TOP_JSON, {"candidates": []})
        gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326").to_file(TOP_GEOJSON, driver="GeoJSON")
        return []
    candidates = candidates.sort_values(
        ["flood_overlap_reduction_m", "distance_delta_m", "flood_feature_count_unique_total"],
        ascending=[False, True, True],
    ).head(5)
    candidates["candidate_rank"] = range(1, len(candidates) + 1)
    candidates["usable_as_first_mile"] = True
    candidates["usable_as_last_mile"] = True
    candidates["connectable_subway_station"] = candidates["station"] + "(" + candidates["station_line"] + ")"
    candidates["end_to_end_candidate_possible"] = True
    candidates.to_file(TOP_GEOJSON, driver="GeoJSON")
    records = []
    for _, row in candidates.drop(columns=["geometry"]).iterrows():
        records.append(row.to_dict())
    write_json(TOP_JSON, {"candidates": records})
    return records


def main() -> None:
    RAW_OSM_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    stations = load_stations()
    facilities = load_facilities()
    bbox = get_osm_bbox(stations, facilities)

    graph, downloaded = build_or_load_graph(bbox)
    nodes, edges = graph_to_geojson(graph)
    network_report = validate_graph(graph, edges, downloaded)
    tag_report = tag_coverage(edges, nodes)
    write_json(NETWORK_VALIDATION_JSON, network_report)
    write_json(TAG_COVERAGE_JSON, tag_report)

    od_candidates = build_od_candidates(stations, facilities)
    snapped_od = snap_od_candidates(graph, od_candidates)

    routes = generate_routes(graph, snapped_od)
    routes = add_flood_metrics(routes)
    routes = add_context_counts(routes)
    compared_routes, route_table = compare_routes(routes)
    save_routes(compared_routes, route_table)
    top_candidates = save_top(compared_routes)

    baseline_count = int((route_table["route_type"] == "baseline").sum()) if not route_table.empty else 0
    alt_count = int(route_table["route_type"].astype(str).str.startswith("alternative").sum()) if not route_table.empty else 0
    od_with_alt = int(route_table[route_table["route_type"].astype(str).str.startswith("alternative")]["od_id"].nunique()) if not route_table.empty else 0
    reduction_count = len(top_candidates)
    max_reduction = max((c["flood_overlap_reduction_m"] for c in top_candidates), default=None)
    best = top_candidates[0] if top_candidates else None

    report = {
        "generated_at": pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d %H:%M:%S"),
        "case": "CASE_A" if top_candidates else "CASE_B",
        "final_judgement": (
            "실제 대체경로와 다년도 침수흔적 중첩 감소 후보가 존재한다."
            if top_candidates
            else "OSM routing은 가능하지만 현재 샘플 검증에서 침수흔적 중첩 감소 후보를 찾지 못했다."
        ),
        "routing_sample_limit": MAX_ROUTING_OD,
        "network": network_report,
        "tag_coverage": tag_report,
        "od_analysis": {
            "station_name_count": int(stations["station"].nunique()),
            "station_line_count": int(len(stations)),
            "actual_facility_count": int(len(facilities)),
            "candidate_od_count": int(len(od_candidates)),
            "routed_od_limit": MAX_ROUTING_OD,
            "routing_success_od_count": int(route_table["od_id"].nunique()) if not route_table.empty else 0,
        },
        "alternative_generation": {
            "baseline_count": baseline_count,
            "alternative_success_od_count": od_with_alt,
            "substantially_different_alternative_count": alt_count,
        },
        "flood_comparison": {
            "baseline_less_overlap_alternative_count_in_top5": reduction_count,
            "max_overlap_reduction_m": max_reduction,
            "best_od": best["od_id"] if best else None,
            "best_added_distance_m": best["distance_delta_m"] if best else None,
        },
        "output_files": [
            rel(NETWORK_VALIDATION_JSON),
            rel(TAG_COVERAGE_JSON),
            rel(OD_CANDIDATES_CSV),
            rel(ROUTES_CSV),
            rel(ROUTES_JSON),
            rel(TOP_JSON),
            rel(TOP_GEOJSON),
            rel(REPORT_JSON),
            rel(GRAPHML_PATH),
            rel(NODES_GEOJSON),
            rel(EDGES_GEOJSON),
        ],
        "limits": [
            "OSM 접근성 tag는 coverage와 의미가 충분히 검증되기 전까지 Risk Score에 사용하지 않는다.",
            "flood_overlap_ratio는 침수확률이 아니라 전체 경로 중 과거 침수흔적 geometry와 중첩되는 길이 비율이다.",
            f"이번 실행은 OD 후보 중 상위 {MAX_ROUTING_OD}개만 routing 검증한다.",
        ],
    }
    write_json(REPORT_JSON, report)

    steps = tag_report["edge_tags"].get("steps", {})
    sidewalk = tag_report["edge_tags"].get("sidewalk", {})
    incline = tag_report["edge_tags"].get("incline", {})
    wheelchair = tag_report["edge_tags"].get("wheelchair", {})
    kerb = tag_report["edge_tags"].get("kerb", {})
    surface = tag_report["edge_tags"].get("surface", {})

    print("## 1. OSM 보행망")
    print(f"* 다운로드 성공 여부: {network_report['download_success']}")
    print(f"* node 수: {network_report['node_count']}")
    print(f"* edge 수: {network_report['edge_count']}")
    print(f"* largest connected component 비율: {network_report['largest_connected_component_ratio']}")
    print(f"* 로컬 GraphML 저장 여부: {network_report['graphml_saved']}")

    print("\n## 2. OSM 접근성 tag coverage")
    print(f"* steps: {steps.get('coverage', 0)}")
    print(f"* sidewalk: {sidewalk.get('coverage', 0)}")
    print(f"* incline: {incline.get('coverage', 0)}")
    print(f"* wheelchair: {wheelchair.get('coverage', 0)}")
    print(f"* kerb: {kerb.get('coverage', 0)}")
    print(f"* surface: {surface.get('coverage', 0)}")

    print("\n## 3. OD 분석")
    print(f"* 역 수: {stations['station'].nunique()}개 역명 / {len(stations)}개 역-노선")
    print(f"* 실제 시설 수: {len(facilities)}")
    print(f"* 후보 OD 수: {len(od_candidates)}")
    print(f"* routing 성공 OD 수: {report['od_analysis']['routing_success_od_count']}")

    print("\n## 4. Alternative 생성")
    print(f"* 복수경로 생성 가능 OD 수: {od_with_alt}")
    print(f"* 실제 geometry가 다른 alternative 총 수: {alt_count}")

    print("\n## 5. 침수흔적 감소 후보")
    print(f"* baseline보다 overlap이 작은 alternative 수: {int((route_table.get('historical_flood_trace_avoiding_candidate', pd.Series(dtype=bool)) == True).sum()) if not route_table.empty else 0}")
    print(f"* 최대 overlap 감소량: {max_reduction}")
    print(f"* 해당 OD: {best['station'] + ' ↔ ' + best['facility_name'] if best else '없음'}")
    print(f"* 추가 이동거리: {best['distance_delta_m'] if best else '없음'}")

    print("\n## 6. MOVE:ON TOP 5")
    if top_candidates:
        for candidate in top_candidates:
            print(f"* {candidate['station']}({candidate['station_line']}) ↔ {candidate['facility_name']}")
            print(
                f"  baseline={candidate['baseline_distance']}m, alternative={candidate['alternative_distance']}m, "
                f"추가={candidate['distance_delta_m']}m, baseline overlap={candidate['baseline_flood_overlap_m']}m, "
                f"alternative overlap={candidate['alternative_flood_overlap_m']}m, 감소={candidate['flood_overlap_reduction_m']}m, "
                f"감소율={candidate['flood_overlap_reduction_ratio']}, first/last-mile=True"
            )
    else:
        print("* TOP 후보 없음")

    print("\n## 7. 최종 판단")
    print(report["final_judgement"])

    print("\nSaved:")
    for output in report["output_files"]:
        print(output)


if __name__ == "__main__":
    main()
