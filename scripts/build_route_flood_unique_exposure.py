from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from build_route_analysis import (
    FLOOD_RADIUS_METERS,
    OUTPUT_DIR,
    PROJECT_ROOT,
    YEARS,
    find_station_geometry,
    load_flood,
    load_routes,
    load_station_master,
    route_station_pairs,
)


ROUTE_STATION_ANALYSIS_CSV = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "route_analysis"
    / "route_station_flood_accessibility.csv"
)
OUTPUT_CSV = OUTPUT_DIR / "route_flood_unique_exposure.csv"
OUTPUT_JSON = OUTPUT_DIR / "route_flood_unique_exposure.json"

FEATURE_ID_CANDIDATES = [
    "fid",
    "FID",
    "id",
    "ID",
    "objectid",
    "OBJECTID",
    "gid",
    "GID",
    "ufid",
    "UFID",
    "관리번호",
    "고유번호",
    "FTR_ID",
    "mng_no",
]


def normalize_route_id(route_id: str) -> str:
    return route_id.strip().lower().replace(" ", "_")


def union_geometries(geometries: list[Any]):
    geo_series = gpd.GeoSeries(geometries, crs="EPSG:5179")
    if hasattr(geo_series, "union_all"):
        return geo_series.union_all()
    return geo_series.unary_union


def choose_feature_id_column(flood_gdf: gpd.GeoDataFrame) -> str:
    for column in FEATURE_ID_CANDIDATES:
        if column not in flood_gdf.columns:
            continue
        values = flood_gdf[column].dropna()
        if len(values) == len(flood_gdf) and values.nunique() == len(flood_gdf):
            return column

    flood_gdf["__feature_index"] = flood_gdf.index.astype(str)
    return "__feature_index"


def load_station_sum_by_route() -> dict[str, int]:
    station_rows = pd.read_csv(ROUTE_STATION_ANALYSIS_CSV, encoding="utf-8-sig")
    station_rows["route_key"] = station_rows["route_id"].map(normalize_route_id)
    station_rows["flood_2022_2025_total_300m"] = pd.to_numeric(
        station_rows["flood_2022_2025_total_300m"], errors="coerce"
    )
    return {
        route_key: int(group["flood_2022_2025_total_300m"].sum())
        for route_key, group in station_rows.groupby("route_key")
    }


def build_route_buffer(
    station_master: pd.DataFrame, route: dict[str, Any]
) -> tuple[Any, list[dict[str, Any]]]:
    station_infos = route_station_pairs(route)
    buffers = []

    for station_info in station_infos:
        station = station_info["station"]
        line = station_info["line"]
        geometry = find_station_geometry(station_master, station, line)
        if geometry is None:
            raise ValueError(f"station coordinate not found: {station} ({line})")

        station_gdf = gpd.GeoDataFrame(
            [{"geometry": geometry}], geometry="geometry", crs="EPSG:4326"
        ).to_crs(epsg=5179)
        buffers.append(station_gdf.geometry.iloc[0].buffer(FLOOD_RADIUS_METERS))

    return union_geometries(buffers), station_infos


def count_unique_flood_features(
    flood_gdf: gpd.GeoDataFrame, route_buffer: Any, feature_id_column: str
) -> dict[str, Any]:
    route_flood = flood_gdf[flood_gdf.intersects(route_buffer)].copy()

    yearly_counts = {}
    for year in YEARS:
        year_flood = route_flood[route_flood["F_YR"] == year]
        yearly_counts[year] = int(year_flood[feature_id_column].dropna().nunique())

    total_unique = int(route_flood[feature_id_column].dropna().nunique())
    yearly_sum = int(sum(yearly_counts.values()))

    return {
        "yearly_counts": yearly_counts,
        "total_unique": total_unique,
        "yearly_unique_sum": yearly_sum,
        "yearly_sum_matches_total": yearly_sum == total_unique,
    }


def build_exposure_rows() -> list[dict[str, Any]]:
    routes = load_routes()
    station_master = load_station_master()
    station_sum_by_route = load_station_sum_by_route()
    flood_gdf = load_flood().copy()
    print("flood columns:", list(flood_gdf.columns))
    feature_id_column = choose_feature_id_column(flood_gdf)

    print("flood feature id column:", feature_id_column)

    rows = []
    for route in routes:
        route_label = route["route_id"]
        route_key = normalize_route_id(route_label)
        route_buffer, station_infos = build_route_buffer(station_master, route)
        exposure = count_unique_flood_features(
            flood_gdf, route_buffer, feature_id_column
        )
        station_sum = station_sum_by_route[route_key]
        unique_total = exposure["total_unique"]

        if unique_total > station_sum:
            raise ValueError(
                f"{route_label}: route unique total ({unique_total}) "
                f"exceeds station sum ({station_sum})"
            )

        row = {
            "route_id": route_key,
            "route_label": route_label,
            "station_sequence": [station_info["station"] for station_info in station_infos],
            "line_sequence": sorted(
                {
                    station_info["line"]
                    for station_info in station_infos
                    if station_info["line"] is not None
                }
            ),
            "station_count": len(station_infos),
            "buffer_radius_m": FLOOD_RADIUS_METERS,
            "station_sum_2022_2025_300m": station_sum,
            "flood_unique_2022_300m": exposure["yearly_counts"][2022],
            "flood_unique_2023_300m": exposure["yearly_counts"][2023],
            "flood_unique_2024_300m": exposure["yearly_counts"][2024],
            "flood_unique_2025_300m": exposure["yearly_counts"][2025],
            "flood_unique_2022_2025_total_300m": unique_total,
            "duplicate_count_removed": station_sum - unique_total,
            "route_buffer_area_m2": float(route_buffer.area),
            "route_buffer_area_km2": float(route_buffer.area / 1_000_000),
            "feature_id_column": feature_id_column,
            "yearly_unique_sum_2022_2025": exposure["yearly_unique_sum"],
            "yearly_sum_matches_total": exposure["yearly_sum_matches_total"],
        }
        rows.append(row)

    return rows


def write_outputs(rows: list[dict[str, Any]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    with OUTPUT_JSON.open("w", encoding="utf-8") as file:
        json.dump(rows, file, ensure_ascii=False, indent=2)


def print_results(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        print(f"=== {row['route_label']} flood exposure ===")
        print(f"station sum: {row['station_sum_2022_2025_300m']}")
        print(f"unique 2022: {row['flood_unique_2022_300m']}")
        print(f"unique 2023: {row['flood_unique_2023_300m']}")
        print(f"unique 2024: {row['flood_unique_2024_300m']}")
        print(f"unique 2025: {row['flood_unique_2025_300m']}")
        print(f"unique total: {row['flood_unique_2022_2025_total_300m']}")
        print(f"duplicates removed: {row['duplicate_count_removed']}")

    print("Saved:")
    print(OUTPUT_CSV.relative_to(PROJECT_ROOT).as_posix())
    print(OUTPUT_JSON.relative_to(PROJECT_ROOT).as_posix())


def main() -> None:
    rows = build_exposure_rows()
    write_outputs(rows)
    print_results(rows)


if __name__ == "__main__":
    main()
