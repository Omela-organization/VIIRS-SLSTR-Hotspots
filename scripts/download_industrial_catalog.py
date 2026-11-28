"""Конвертация каталога Ma et al. 2024 из shapefile в GeoParquet (HS-03)."""
from pathlib import Path

import geopandas as gpd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "external" / "catalogs" / "raw_ma2024"
OUTPUT_PATH = PROJECT_ROOT / "data" / "external" / "catalogs" / "industrial_heat_sources_ma2024.parquet"

SHP_PATH = RAW_DIR / "industrial_heat_sources.shp"


def main():
    if not SHP_PATH.exists():
        print(f"Файл не найден: {SHP_PATH}")
        return

    print(f"Читаю shapefile: {SHP_PATH.name}")
    gdf = gpd.read_file(SHP_PATH)

    print(f"Записей: {len(gdf)}")
    print(f"Геометрия: {gdf.geom_type.unique()}")
    print(f"CRS: {gdf.crs}")
    print(f"Столбцы: {list(gdf.columns)}")

    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        print("Преобразую CRS в EPSG:4326")
        gdf = gdf.to_crs("EPSG:4326")

    gdf.to_parquet(OUTPUT_PATH)
    print(f"\nСохранено: {OUTPUT_PATH}")
    print(f"Размер: {OUTPUT_PATH.stat().st_size / 1e6:.1f} МБ")

    gdf_back = gpd.read_parquet(OUTPUT_PATH)
    print(f"Проверка чтения: {len(gdf_back)} записей")


if __name__ == "__main__":
    main()