# Внешние слои и тестовые наборы

**Задача:** HS-03

---

## Сводная таблица

| Слой / набор | Путь | Формат | Источник |
|---|---|---|---|
| Лесная маска | `data/external/masks/forest_mask_30m.tif` | GeoTIFF | Hansen GFC 30 м |
| Каталог промышленных источников | `data/external/catalogs/industrial_heat_sources_ma2024.parquet` | GeoParquet | Ma et al., 2024 |
| Эталонная сетка 240 м | `data/external/grids/ReferenceSpatialGrid.nc` | NetCDF | сгенерирована |
| Кригинг-модель | `external/omelagrid_kriging/` | Python | netcdf-kriging-scaler |
| Мини-набор пролётов | `data/test_sets/mini/` | HDF5, NetCDF | NASA Earthdata |
| Т-2 «Чистые сцены» | `data/test_sets/T2_clean_scenes/` | - | критерии в `docs/test_sets_criteria.md` |
| Т-4 «Эталонные пожары» | `data/test_sets/T4_reference_fires/` | - | критерии в `docs/test_sets_criteria.md` |
| Т-5 «Гари» | `data/test_sets/T5_burn_scars/` | - | критерии в `docs/test_sets_criteria.md` |

Бинарные данные в git не попадают (`.gitignore` на `data/`). В репозитории - скрипты подготовки и документация.

---

## Лесная маска (Д-6)

`data/external/masks/forest_mask_30m.tif` - Hansen GFC, 30 м, Приморский край. Бинарная: 1 - лес, 0 - не лес.

---

## Каталог промышленных источников Ma et al. 2024

`data/external/catalogs/industrial_heat_sources_ma2024.parquet` - 25 544 источника, EPSG:4326, поля: геометрия, `Type`, `Nation`, `Points_num`, `Min_date`, `Max_date`. DOI: 10.1038/s41597-024-03461-3.

---

## Эталонная сетка 240 м

`data/external/grids/ReferenceSpatialGrid.nc`

| Параметр | Значение |
|---|---|
| Разрешение | 240 м (8 x 8 пикселей HLS 30 м) |
| Проекция | EPSG:4326 |
| Покрытие | 42.0-50.0°N, 130.0-140.0°E (Приморский край) |
| Размер | 3710 x 3221 = 11 950 910 ячеек |
| Переменная | `grid8_mask` (int8, 1 - активная, -128 - вне полигона) |
| Координаты | `y` (широта, север-юг), `x` (долгота, запад-восток) |
| Атрибуты | `crs`, `cell_size_m=240`, `hls_pixel_m=30`, `grid_pixels=8`, `step_lat_deg`, `step_lon_deg`, `mid_lat_ref` |

Генерируется в`scripts/generate_referense_grid.py`. 

---

## Кригинг-модель

`external/omelagrid_kriging/omelagrid_kriging/`. Файлы: `__init__.py`, `__main__.py`, `core.py`, `validation.py`.

Главный класс `SupportAwareKrigingScaler`, режимы `upscale`/`downscale`/`auto`, метод upscale `center`. 

---

## Мини-набор тестовых пролётов

`data/test_sets/mini/` - 16 пролётов, 6 сценариев, 749.8 МБ. Область: Приморский край (130-140°E, 42-50°N).

| Сценарий | Продукт | Файлов | Размер | Период |
|---|---|---|---|---|
| fire | VNP14IMG | 3 | 6.3 МБ | июль-август 2024 |
| clear | VNP09GA | 3 | 302.3 МБ | июнь 2024 |
| snow | VNP10A1 | 2 | 1.9 МБ | январь-февраль 2024 |
| cloud | VNP09GA | 3 | 224.3 МБ | июль 2024 |
| smoke | VNP14IMG | 3 | 5.3 МБ | август 2024 |
| swath_edge | VNP09GA | 2 | 209.7 МБ | июнь 2024 |

Скачивается `scripts/download_test_sets.py` (идемпотентен, пропускает уже скачанные). Авторизация через `earthaccess.login(strategy="interactive")`.

---

## Наборы Т-2, Т-4, Т-5

Папки созданы, критерии отбора зафиксированы в `docs/test_sets_criteria.md`. Состав формируется на этапах 1 и 5 по критериям ТЗ 10.1.

---

## Скрипты подготовки

Все скрипты идемпотентны: проверяют наличие результата и пропускают работу, если файл уже есть.

| Скрипт | Что делает | Выход | Идемпотентность |
|---|---|---|---|
| `scripts/download_forest_mask.py` | Скачивает Hansen GFC 30 м и обрезает по Приморью | `data/external/masks/forest_mask_30m.tif` | пропускает при наличии |
| `scripts/download_industrial_catalog.py` | Скачивает и конвертирует каталог Ma et al. 2024 в GeoParquet | `data/external/catalogs/industrial_heat_sources_ma2024.parquet` (25 544 записей) | пропускает при наличии |
| `scripts/generate_referense_grid.py` | Генерирует эталонную сетку 240 м в EPSG:4326 | `data/external/grids/ReferenceSpatialGrid.nc` (3710 x 3221, 11.95 млн ячеек) | перезаписывает файл |
| `scripts/download_test_sets.py` | Скачивает мини-набор пролётов VIIRS (16 файлов, 6 сценариев) | `data/test_sets/mini/{fire,clear,snow,cloud,smoke,swath_edge}/` | пропускает уже скачанные |
| `scripts/inspect_reference_grid.py` | Визуализирует покрытие эталонной сетки | `docs/figures/reference_grid_coverage.png` | перезаписывает файл |
