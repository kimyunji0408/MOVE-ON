from pathlib import Path

import geopandas as gpd
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "flood"
OUTPUT_DIR = PROJECT_ROOT / "data" / "processed" / "flood"

YEARS = [2022, 2023, 2024, 2025]
COMMON_COLUMNS = [
    "F_YR",
    "F_SHIM",
    "F_AREA",
    "F_AVR_HGT",
    "ADM_CD",
    "F_ZONE_NM",
    "TYPE",
    "geometry",
]
NUMERIC_COLUMNS = ["F_SHIM", "F_AREA", "F_AVR_HGT"]


def find_shapefile(year: int) -> Path:
    year_dir = RAW_DIR / str(year)
    shapefiles = sorted(year_dir.rglob("*.shp"))

    if not shapefiles:
        raise FileNotFoundError(f"No shapefile found in {year_dir}")

    if len(shapefiles) > 1:
        print(f"[{year}] multiple shapefiles found; using {shapefiles[0]}")

    return shapefiles[0]


def read_shapefile(path: Path) -> gpd.GeoDataFrame:
    errors = []

    for encoding in [None, "utf-8", "cp949", "euc-kr"]:
        try:
            if encoding is None:
                return gpd.read_file(path)
            return gpd.read_file(path, encoding=encoding)
        except Exception as exc:
            errors.append(f"{encoding or 'default'}: {exc}")

    raise RuntimeError(
        f"Failed to read shapefile {path}\n" + "\n".join(errors)
    )


def prepare_year(year: int) -> gpd.GeoDataFrame:
    shapefile = find_shapefile(year)
    gdf = read_shapefile(shapefile)
    before_count = len(gdf)

    missing_columns = [col for col in COMMON_COLUMNS if col not in gdf.columns]
    if missing_columns:
        raise KeyError(f"{year} missing columns: {missing_columns}")

    gdf["F_YR"] = pd.to_numeric(gdf["F_YR"], errors="coerce")
    gdf = gdf[gdf["F_YR"] == year].copy()
    after_count = len(gdf)

    for column in NUMERIC_COLUMNS:
        gdf[column] = pd.to_numeric(gdf[column], errors="coerce")

    gdf = gdf[COMMON_COLUMNS].copy()

    if gdf.crs is None:
        raise ValueError(f"{year} shapefile has no CRS: {shapefile}")

    gdf = gdf.to_crs(epsg=4326)

    output_file = OUTPUT_DIR / f"seoul_flood_trace_{year}.geojson"
    gdf.to_file(output_file, driver="GeoJSON")

    print(f"\n[{year}] {shapefile}")
    print(f"  before filter: {before_count:,}")
    print(f"  after filter : {after_count:,}")
    print(f"  saved        : {output_file}")

    return gdf


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    yearly_data = []
    for year in YEARS:
        yearly_data.append(prepare_year(year))

    combined = gpd.GeoDataFrame(
        pd.concat(yearly_data, ignore_index=True),
        crs="EPSG:4326",
    )

    combined_file = OUTPUT_DIR / "seoul_flood_trace_2022_2025.geojson"
    combined.to_file(combined_file, driver="GeoJSON")

    yearly_counts = (
        combined["F_YR"]
        .astype("Int64")
        .value_counts()
        .sort_index()
    )

    print("\n=== Combined flood traces ===")
    print(f"total count: {len(combined):,}")
    print("counts by year:")
    for year, count in yearly_counts.items():
        print(f"  {year}: {count:,}")
    print(f"saved: {combined_file}")


if __name__ == "__main__":
    main()
