from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUBWAY_FILE = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "subway"
    / "\uc11c\uc6b8\uc2dc \uc5ed\uc0ac\ub9c8\uc2a4\ud130 \uc815\ubcf4.csv"
)
FLOOD_FILE = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "flood"
    / "seoul_flood_trace_2022_2025.geojson"
)

STATION_NAME = "\uc5ed\uc0ac\uba85"
LINE = "\ud638\uc120"
LATITUDE = "\uc704\ub3c4"
LONGITUDE = "\uacbd\ub3c4"
ACTIVE_YEAR_COUNT = "\uce68\uc218\ud754\uc801 \uc874\uc7ac \uc5f0\ub3c4 \uc218"

CANDIDATE_NAMES = [
    "\ub179\ubc88",
    "\ub3c5\ubc14\uc704",
    "\ubd88\uad11",
    "\uc5f0\uc2e0\ub0b4",
    "\uc751\uc554",
]
SUBWAY_LINES = ["3\ud638\uc120", "6\ud638\uc120"]
YEARS = [2022, 2023, 2024, 2025]
DISTANCES = [100, 300, 500]


def read_station_master() -> pd.DataFrame:
    errors = []

    for encoding in ["utf-8-sig", "utf-8", "cp949", "euc-kr"]:
        try:
            return pd.read_csv(SUBWAY_FILE, encoding=encoding)
        except Exception as exc:
            errors.append(f"{encoding}: {exc}")

    raise RuntimeError(
        f"Failed to read station master {SUBWAY_FILE}\n" + "\n".join(errors)
    )


def build_candidate_stations(station_df: pd.DataFrame) -> gpd.GeoDataFrame:
    required_columns = [STATION_NAME, LINE, LATITUDE, LONGITUDE]
    missing_columns = [
        column for column in required_columns if column not in station_df.columns
    ]
    if missing_columns:
        raise KeyError(f"station master missing columns: {missing_columns}")

    station_df = station_df[
        station_df[STATION_NAME].isin(CANDIDATE_NAMES)
        & station_df[LINE].isin(SUBWAY_LINES)
    ].copy()

    station_df[LATITUDE] = pd.to_numeric(station_df[LATITUDE], errors="coerce")
    station_df[LONGITUDE] = pd.to_numeric(station_df[LONGITUDE], errors="coerce")
    station_df = station_df.dropna(subset=[LATITUDE, LONGITUDE])

    stations = station_df.groupby(STATION_NAME, as_index=False).agg(
        {LATITUDE: "mean", LONGITUDE: "mean"}
    )
    stations[STATION_NAME] = pd.Categorical(
        stations[STATION_NAME],
        categories=CANDIDATE_NAMES,
        ordered=True,
    )
    stations = stations.sort_values(STATION_NAME).reset_index(drop=True)
    stations[STATION_NAME] = stations[STATION_NAME].astype(str)

    found = set(stations[STATION_NAME])
    missing = [name for name in CANDIDATE_NAMES if name not in found]
    if missing:
        raise ValueError(f"candidate stations not found: {missing}")

    return gpd.GeoDataFrame(
        stations,
        geometry=[
            Point(lon, lat)
            for lon, lat in zip(stations[LONGITUDE], stations[LATITUDE])
        ],
        crs="EPSG:4326",
    )


def count_intersections(
    flood_gdf: gpd.GeoDataFrame,
    station_geometry,
    distance_meters: int,
) -> int:
    buffer_area = station_geometry.buffer(distance_meters)
    return int(flood_gdf.intersects(buffer_area).sum())


def main() -> None:
    station_df = read_station_master()
    station_gdf = build_candidate_stations(station_df).to_crs(epsg=5179)

    flood_gdf = gpd.read_file(FLOOD_FILE)
    flood_gdf["F_YR"] = pd.to_numeric(flood_gdf["F_YR"], errors="coerce")
    flood_gdf = flood_gdf.to_crs(epsg=5179)

    print("=== Candidate station flood traces by distance ===")
    for _, station in station_gdf.iterrows():
        counts = {
            distance: count_intersections(flood_gdf, station.geometry, distance)
            for distance in DISTANCES
        }
        print(
            f"{station[STATION_NAME]}: "
            f"100m={counts[100]:,}, "
            f"300m={counts[300]:,}, "
            f"500m={counts[500]:,}"
        )

    print("\n=== Candidate station flood traces by year within 300m ===")
    rows = []
    for _, station in station_gdf.iterrows():
        row = {STATION_NAME: station[STATION_NAME]}
        buffer_area = station.geometry.buffer(300)

        for year in YEARS:
            year_flood = flood_gdf[flood_gdf["F_YR"] == year]
            row[year] = int(year_flood.intersects(buffer_area).sum())

        row[ACTIVE_YEAR_COUNT] = sum(row[year] > 0 for year in YEARS)
        rows.append(row)

    result_df = pd.DataFrame(rows).set_index(STATION_NAME)
    print(result_df.to_string())


if __name__ == "__main__":
    main()
