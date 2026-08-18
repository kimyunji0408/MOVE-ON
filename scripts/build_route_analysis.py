from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point


PROJECT_ROOT = Path(__file__).resolve().parents[1]

ROUTE_FILES = [
    PROJECT_ROOT
    / "data"
    / "processed"
    / "routes"
    / "route_a_bulgwang_yeonsinnae_direct.json",
    PROJECT_ROOT
    / "data"
    / "processed"
    / "routes"
    / "route_b_bulgwang_dokbawi_yeonsinnae.json",
]
ACCESSIBILITY_FILE = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "accessibility"
    / "candidate_station_accessibility.csv"
)
SUBWAY_MASTER_FILE = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "subway"
    / "서울시 역사마스터 정보.csv"
)
FLOOD_FILE = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "flood"
    / "seoul_flood_trace_2022_2025.geojson"
)
OUTPUT_DIR = PROJECT_ROOT / "data" / "processed" / "route_analysis"
OUTPUT_CSV = OUTPUT_DIR / "route_station_flood_accessibility.csv"
OUTPUT_JSON = OUTPUT_DIR / "route_station_flood_accessibility.json"
OUTPUT_SUMMARY = OUTPUT_DIR / "route_comparison_summary.json"

YEARS = [2022, 2023, 2024, 2025]
FLOOD_RADIUS_METERS = 300

STATION_NAME = "역사명"
LINE = "호선"
LATITUDE = "위도"
LONGITUDE = "경도"


def read_csv_with_fallback(path: Path) -> pd.DataFrame:
    errors = []
    for encoding in ["utf-8-sig", "utf-8", "cp949", "euc-kr"]:
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise RuntimeError(f"Failed to read CSV: {path}\n" + "\n".join(errors))


def canonical_station(value: Any) -> str:
    station = str(value).strip()
    return station[:-1] if station.endswith("역") else station


def canonical_line(value: Any) -> str:
    line = str(value).strip()
    match = re.search(r"(\d+)", line)
    if match:
        return f"{int(match.group(1))}호선"
    return line


def load_routes() -> list[dict[str, Any]]:
    routes = []
    for path in ROUTE_FILES:
        with path.open(encoding="utf-8") as file:
            route = json.load(file)
        route["_source_file"] = str(path)
        routes.append(route)
    return routes


def route_station_pairs(route: dict[str, Any]) -> list[dict[str, Any]]:
    stations = [canonical_station(station) for station in route["station_sequence"]]
    segments = route.get("segments", [])
    line_sequence = [canonical_line(line) for line in route.get("line_sequence", [])]

    pairs = []
    for index, station in enumerate(stations):
        line = None
        if segments:
            if index == 0:
                line = segments[0].get("line")
            elif index - 1 < len(segments):
                line = segments[index - 1].get("line")
        if line is None and len(line_sequence) == 1:
            line = line_sequence[0]
        elif line is None and index < len(line_sequence):
            line = line_sequence[index]

        pairs.append(
            {
                "station_order": index + 1,
                "station": station,
                "line": canonical_line(line) if line is not None else None,
            }
        )

    return pairs


def load_station_master() -> pd.DataFrame:
    station_df = read_csv_with_fallback(SUBWAY_MASTER_FILE)
    missing_columns = [
        column
        for column in [STATION_NAME, LINE, LATITUDE, LONGITUDE]
        if column not in station_df.columns
    ]
    if missing_columns:
        raise KeyError(f"station master missing columns: {missing_columns}")

    station_df = station_df.copy()
    station_df["station_key"] = station_df[STATION_NAME].map(canonical_station)
    station_df["line_key"] = station_df[LINE].map(canonical_line)
    station_df[LATITUDE] = pd.to_numeric(station_df[LATITUDE], errors="coerce")
    station_df[LONGITUDE] = pd.to_numeric(station_df[LONGITUDE], errors="coerce")
    return station_df.dropna(subset=[LATITUDE, LONGITUDE])


def load_accessibility() -> dict[tuple[str, str], dict[str, Any]]:
    accessibility_df = read_csv_with_fallback(ACCESSIBILITY_FILE)
    accessibility_df = accessibility_df.copy()
    accessibility_df["station_key"] = accessibility_df["station_name"].map(
        canonical_station
    )
    accessibility_df["line_key"] = accessibility_df["line_name"].map(canonical_line)

    grouped = {}
    for (station, line), group in accessibility_df.groupby(["station_key", "line_key"]):
        elevator_count = int((group["facility_type"] == "elevator").sum())
        lift_count = int((group["facility_type"] == "wheelchair_lift").sum())
        operation_values = sorted(
            {
                str(value).strip()
                for value in group.get("operation_status", pd.Series(dtype=str)).dropna()
                if str(value).strip()
            }
        )

        grouped[(station, line)] = {
            "elevator_available": elevator_count > 0,
            "wheelchair_lift_available": lift_count > 0,
            "elevator_count": elevator_count,
            "wheelchair_lift_count": lift_count,
            "operation_status_values_raw": operation_values,
            "accessibility_record_count": int(len(group)),
        }

    return grouped


def load_flood() -> gpd.GeoDataFrame:
    flood_gdf = gpd.read_file(FLOOD_FILE)
    if flood_gdf.crs is None:
        flood_gdf = flood_gdf.set_crs(epsg=4326)
    flood_gdf["F_YR"] = pd.to_numeric(flood_gdf["F_YR"], errors="coerce")
    flood_gdf = flood_gdf[flood_gdf["F_YR"].isin(YEARS)].copy()
    return flood_gdf.to_crs(epsg=5179)


def find_station_geometry(
    station_master: pd.DataFrame, station: str, line: str | None
) -> Point | None:
    matches = station_master[station_master["station_key"] == station]
    if line:
        line_matches = matches[matches["line_key"] == line]
        if not line_matches.empty:
            matches = line_matches

    if matches.empty:
        return None

    latitude = float(matches[LATITUDE].mean())
    longitude = float(matches[LONGITUDE].mean())
    return Point(longitude, latitude)


def flood_counts_by_year(
    flood_gdf: gpd.GeoDataFrame, geometry_4326: Point | None
) -> dict[int, int | None]:
    if geometry_4326 is None:
        return {year: None for year in YEARS}

    station_gdf = gpd.GeoDataFrame(
        [{"geometry": geometry_4326}], geometry="geometry", crs="EPSG:4326"
    ).to_crs(epsg=5179)
    buffer_area = station_gdf.geometry.iloc[0].buffer(FLOOD_RADIUS_METERS)

    counts = {}
    for year in YEARS:
        year_flood = flood_gdf[flood_gdf["F_YR"] == year]
        counts[year] = int(year_flood.intersects(buffer_area).sum())
    return counts


def build_rows(
    routes: list[dict[str, Any]],
    station_master: pd.DataFrame,
    accessibility: dict[tuple[str, str], dict[str, Any]],
    flood_gdf: gpd.GeoDataFrame,
) -> list[dict[str, Any]]:
    rows = []
    for route in routes:
        route_id = route["route_id"]
        for station_info in route_station_pairs(route):
            station = station_info["station"]
            line = station_info["line"]
            geometry = find_station_geometry(station_master, station, line)
            flood_counts = flood_counts_by_year(flood_gdf, geometry)
            access = accessibility.get((station, line or ""))

            row = {
                "route_id": route_id,
                "station_order": station_info["station_order"],
                "station": station,
                "line": line,
                "flood_2022_count_300m": flood_counts[2022],
                "flood_2023_count_300m": flood_counts[2023],
                "flood_2024_count_300m": flood_counts[2024],
                "flood_2025_count_300m": flood_counts[2025],
                "flood_2022_2025_total_300m": (
                    None
                    if any(value is None for value in flood_counts.values())
                    else int(sum(flood_counts.values()))
                ),
                "elevator_available": None,
                "wheelchair_lift_available": None,
                "accessibility_status": "unknown",
            }

            if access is not None:
                row.update(access)
                row["accessibility_status"] = "available_data"

            rows.append(row)

    return rows


def build_summary(
    routes: list[dict[str, Any]], station_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows_by_route = {
        route["route_id"]: [
            row for row in station_rows if row["route_id"] == route["route_id"]
        ]
        for route in routes
    }

    summaries = []
    for route in routes:
        route_id = route["route_id"]
        rows = rows_by_route[route_id]
        summaries.append(
            {
                "route_id": route_id,
                "station_count": len(rows),
                "total_distance": route.get("total_distance"),
                "total_required_time_raw": route.get("total_required_time_raw"),
                "flood_trace_total_2022_2025": int(
                    sum(row["flood_2022_2025_total_300m"] or 0 for row in rows)
                ),
                "stations_with_elevator": int(
                    sum(row["elevator_available"] is True for row in rows)
                ),
                "stations_with_wheelchair_lift": int(
                    sum(row["wheelchair_lift_available"] is True for row in rows)
                ),
                "unknown_accessibility_count": int(
                    sum(row["accessibility_status"] == "unknown" for row in rows)
                ),
            }
        )

    return summaries


def write_outputs(
    station_rows: list[dict[str, Any]], summaries: list[dict[str, Any]]
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(station_rows).to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    with OUTPUT_JSON.open("w", encoding="utf-8") as file:
        json.dump(station_rows, file, ensure_ascii=False, indent=2)

    with OUTPUT_SUMMARY.open("w", encoding="utf-8") as file:
        json.dump(summaries, file, ensure_ascii=False, indent=2)


def print_route_rows(station_rows: list[dict[str, Any]]) -> None:
    for route_id in dict.fromkeys(row["route_id"] for row in station_rows):
        print(f"=== {route_id} ===")
        for row in [item for item in station_rows if item["route_id"] == route_id]:
            print(f"station: {row['station']} ({row['line']})")
            print(
                "flood traces 2022~2025: "
                f"{row['flood_2022_2025_total_300m']}"
            )
            print(f"elevator: {row['elevator_available']}")
            print(f"wheelchair lift: {row['wheelchair_lift_available']}")


def main() -> None:
    routes = load_routes()
    station_master = load_station_master()
    accessibility = load_accessibility()
    flood_gdf = load_flood()

    station_rows = build_rows(routes, station_master, accessibility, flood_gdf)
    summaries = build_summary(routes, station_rows)
    write_outputs(station_rows, summaries)

    print_route_rows(station_rows)
    print("Saved:")
    print(OUTPUT_CSV.relative_to(PROJECT_ROOT).as_posix())
    print(OUTPUT_JSON.relative_to(PROJECT_ROOT).as_posix())
    print(OUTPUT_SUMMARY.relative_to(PROJECT_ROOT).as_posix())


if __name__ == "__main__":
    main()
