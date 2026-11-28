# Интеграция кригинг-модели (HS-03, M10)

---

## 1. Назначение

Кригинг-модель вызывается один раз в конце конвейера (модуль M10) для перевода 
готовых полей с рабочей сетки 375 м на эталонную 240 м (ТЗ 5.11, 6.8). 

---

## 2. Структура кода

```
external/omelagrid_kriging/
└── omelagrid_kriging/
    ├── __init__.py          # экспорт SupportAwareKrigingScaler
    ├── __main__.py          # CLI
    ├── core.py              # класс SupportAwareKrigingScaler
    └── validation.py        # проверки
```

**Зависимости:** `xarray`, `dask`, `numba`, `pyproj`, `scipy`, опционально `pyinterpolate`.

---

## 3. Интерфейс

### 3.1. Главный класс

```python
from omelagrid_kriging import SupportAwareKrigingScaler, ScalingConfig

scaler = SupportAwareKrigingScaler(
    ds_in=ds_375m,
    ds_ref=ds_240m,
    variable="frp",
    config=ScalingConfig(
        mode="upscale",
        k_neighbors=16,
        max_search_radius=None,
        variogram_sample_size=4000,
        variogram_lags=12,
        support_points_per_axis=3,
        coherence_correction=True,
        use_target_mask=True,
        upscale_observation_model="center",
    ),
)

result = scaler.run()
```

### 3.2. Входные данные

**`ds_in`** - `xr.Dataset` с данными на рабочей сетке 375 м:
- Координаты: `longitude`/`lon`/`x` (1D), `latitude`/`lat`/`y` (1D)
- Переменная: `variable` (2D или 3D с осью `time`)
- Тип: `float32`

**`ds_ref`** - `xr.Dataset` с описанием эталонной сетки 240 м:
- Координаты: `y` (широта), `x` (долгота)
- Переменная: `grid8_mask` (int8, 1 = активная ячейка, -128 = вне полигона)
- Атрибуты: `crs`, `cell_size_m`, `hls_pixel_m`, `grid_pixels`, `step_lat_deg`, `step_lon_deg`, `mid_lat_ref`

### 3.3. Выходные данные

`xr.Dataset` с данными на эталонной сетке 240 м:
- Координаты: `y`, `x` (из `ds_ref`)
- Переменная: `{variable}_{mode}_scaled` (например, `frp_upscale_scaled`)
- Тип: `float32`
- Атрибуты: `scaling_mode`, `support_model`, `variogram_params`, `reference_grid`

### 3.4. Режимы и методы upscale

| Режим | Описание |
|---|---|
| `auto` | Автоматический выбор по разрешению |
| `upscale` | Мелкая -> крупная сетка (375 м -> 240 м) |
| `downscale` | Крупная -> мелкая сетка |

| Метод upscale | Описание |
|---|---|
| `center` | Обычный кригинг по центрам ячеек (используется в комплексе) |
| `area` | Area-to-point кригинг |
| `elevation-adjusted` | Кригинг с поправкой на рельеф (требует DEM) |
| `terrain-regression-blend` | Смесь кригинга и регрессии на рельеф (требует DEM) |

---



## 6. Совместимость с сеткой

| Требование модели | Наша сетка | Статус |
|---|---|---|
| `detect_grid()` ищет `("longitude", "lon", "x")` и `("latitude", "lat", "y")` | `y`, `x` | OK |
| `_reference_mask()` читает `grid8_mask` > 0 | `grid8_mask` = 1 | OK |
| `CoordinateTransformer` переводит градусы в метры через UTM | EPSG:4326 | OK |
| CLI принимает `--reference ReferenceSpatialGrid.nc` | файл по пути | OK |

---

## 8. Проверки (5.11 и 9.1)

1. **Перекрёстная проверка:** исключение точек из входа, сравнение предсказания с истинным значением. Ошибка должна быть в пределах дисперсии кригинга.
2. **Контроль суммы FRP:** после нормировки сумма FRP совпадает с суммой на рабочей сетке до 10^-6.
3. **Контроль дисперсии:** дисперсия кригинга `s_L_var` растёт у облаков и в областях с малым числом наблюдений.

---

## 9. Ограничения и риски

| Риск | Последствие | Мера |
|---|---|---|
| Р-18: модель недоступна | Остановка M10 | Резервный режим: билинейная интерполяция |
| Р-22: размазывание очагов | Расхождение растра 240 м с таблицей | Нормировка суммы FRP |
| Р-25: медленная работа | Долгий ответ на запрос | Кэш, подготовка последних суток заранее |
| Р-26: несовпадение сетки | Несовпадение центров ячеек | Координаты копируются из файла сетки |