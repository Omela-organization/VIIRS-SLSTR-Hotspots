# JSON-контракт HS-01

Документ описывает три JSON-представления подсистемы HS-01:

1. запись обнаружения `DetectionRecord`;
2. результат проверки одной записи `DetectionValidationResult`;
3. отчёт качества `QualityReportData`, включая обязательную секцию `validation`.

Модель записи сверена с таблицей 3.1 и правилами хранения GeoParquet из раздела 6.5.4 ТЗ VIIRS-SLSTR-Hotspots v7. Типы, категории и ограничения ниже соответствуют моделям в `viirs_slstr_hotspots/store/schemas.py`. Физическую запись GeoParquet этот модуль не выполняет.

## 1. Запись обнаружения

Все поля обязательны, кроме `event_id`, `b_hist` и `q_hist`. `event_id` допускает `null`, пока обнаружение не связано с событием. `b_hist` и `q_hist` допускают `null` при отсутствии истории; в этом случае `n_hist` должен быть равен нулю. При `n_hist > 0` оба значения обязательны.

| Поле | Тип | Ограничения и смысл |
|---|---|---|
| `det_id` | uint64 | детерминированный ID из платформы, времени, строки и столбца; модель проверяет диапазон |
| `platform` | перечисление строк | `NOAA-20`, `NOAA-21`, `S-NPP`, `S3A`, `S3B`, `S3C` |
| `sensor_view` | перечисление строк | `VIIRS_I`, `VIIRS_M`, `SLSTR_nadir`, `SLSTR_oblique` |
| `t_utc` | datetime | UTC, точность до миллисекунд; JSON-вход использует RFC 3339 с `Z` или `+00:00`; числовые метки времени не принимаются |
| `lat`, `lon` | float64 | конечные числа, центр пикселя, градусы |
| `footprint` | Polygon/WKB | WKB геометрии; координаты EPSG:4326 в хранилище; JSON-представление WKB — base64 |
| `granule` | string | идентификатор гранулы |
| `line`, `sample` | int32 | положение пикселя в грануле |
| `area_m2` | float32 | площадь пикселя, м² |
| `scan_angle`, `vza`, `sza`, `raa`, `glint_angle` | float32 | углы в градусах |
| `stratum` | string | страта раздела 5.7.2; допустимые значения зависят от объединения страт |
| `T_mir`, `T_tir` | float32 | яркостные температуры, K |
| `mir_channel` | перечисление строк | `I4`, `M13`, `S7`, `F1` |
| `r_block`, `r_win` | float32 | остатки относительно блочного и оконного фона, K |
| `s_win`, `h_lev` | float32 | масштаб шума окна, K; рычаг пикселя, безразмерный |
| `b_hist`, `q_hist` | float32 или null | медиана и масштаб истории, K; null при `n_hist == 0` |
| `n_hist` | integer | неотрицательное число исторических наблюдений |
| `s_eff`, `z` | float32 | эффективный масштаб шума, K; нормированный остаток |
| `p_value` | float32 | вероятность, `0 < p_value <= 1` |
| `nfa_pixel`, `nfa_cluster`, `nfa_multi` | float32 | число ложных тревог |
| `significance` | float32 | значимость `-lg(min NFA)` |
| `decision` | перечисление строк | `confirmed`, `needs_confirmation`, `rejected` |
| `frp_mw`, `frp_err_mw`, `frp_toa_mw`, `frp_50_mw` | float32 | мощность и ошибка FRP, МВт |
| `flags` | uint32 | битовая маска качества |
| `event_id` | string или null | идентификатор связанного события |
| `source_class` | перечисление строк | `vegetation_fire`, `persistent_industrial`, `persistent_natural_candidate`, `recurrent_seasonal`, `volcano_candidate`, `unknown` |

Неизвестные поля запрещены. Категории ограничены значениями из таблицы 3.1. Страта остаётся строкой: ТЗ описывает её состав и динамическое объединение, но не задаёт закрытый список сериализуемых имён.

Измерительные числа проверяются на диапазон float32; координаты имеют формат float64 и не могут быть `NaN` или бесконечностью. Pydantic хранит числовые значения как Python `float`; будущая запись GeoParquet должна использовать физические типы float32/float64 из ТЗ. Внутри Python-модели `footprint` — байты WKB, в JSON — base64. WKB проверяется на корректность и тип `Polygon`; CRS кодируется метаданными GeoParquet, а не самими байтами WKB.

`t_utc` внутри модели timezone-aware и хранит время UTC с точностью до миллисекунд. Для GeoParquet ТЗ требует UTC timestamp без часового пояса; преобразование к физическому типу GeoParquet должен выполнять слой записи, которого в текущей подсистеме нет.

Алгоритм формирования `det_id` в ТЗ описан как комбинация платформы, времени, строки и столбца, но раскладка битов не задана. Модель проверяет только uint64; она не генерирует и не подтверждает детерминированность идентификатора.

## 2. Результат проверки записи

Результат всегда содержит `valid`. Для валидной записи `record` заполнен, а `errors` пуст; для невалидной записи `record` равен `null` и `errors` содержит как минимум одну ошибку. `warnings` могут сопровождать валидную запись. `errors` и `warnings` — массивы; при отсутствии элементов они сериализуются как пустые массивы.

Ошибка включает код, человекочитаемое имя, путь поля (или `null` для ошибки на уровне всей записи), исходный тип ошибки Pydantic, сообщение, серьёзность и признак возможности продолжить обработку. Исходные значения входных полей в ошибку не копируются.

`can_continue` равен `true` для ошибок отдельной записи: запись пропускается, обработка следующей записи может продолжаться. Ошибка сохранения отчёта не является ошибкой отдельной записи и пробрасывается вызывающему коду.

## 3. Секция отчёта качества

`QualityReportData` требует секцию `validation`:

| Поле | Тип | Значение |
|---|---|---|
| `validation.valid_count` | integer | число валидных записей, неотрицательное |
| `validation.invalid_count` | integer | число невалидных записей, неотрицательное |
| `validation.errors` | массив ошибок | ошибки всех невалидных записей |
| `validation.warnings` | массив предупреждений | предупреждения проверок |

Текущая проверка записи не генерирует предупреждений, но модель результата и отчёт поддерживают их агрегацию. ТЗ v7, раздел 3.5 и ФТ-17, требует также счётчики пикселей по шагам, параметры хвоста и пороги по стратам, контроль Д-4, результаты отложенной половины, время и пиковую память шагов. ТЗ не фиксирует ключи и вложенность этих полей; `QualityReport.set()` допускает их добавление, но HS-01 не вычисляет их. Полная схема остаётся открытой (см. CQ-07 в [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)).

`QualityReport.flush()` формирует UTF-8 JSON и заменяет файл атомарно через временный файл в том же каталоге. При ошибке сериализации или записи она логируется и передаётся вызывающему коду; прежний отчёт не заменяется неудачной записью.

## 4. Примеры

### Валидная запись

Поле `footprint` содержит base64 от WKB Polygon. Значение приведено для примера.

```json
{
  "det_id": 42,
  "platform": "NOAA-20",
  "sensor_view": "VIIRS_I",
  "t_utc": "2026-11-22T00:00:00Z",
  "lat": 55.5,
  "lon": 37.5,
  "footprint": "AQMAAAABAAAABQAAAAAAAAAAgEJAAAAAAACAS0AAAAAAAABDQAAAAAAAgEtAAAAAAAAAQ0AAAAAAAABMQAAAAAAAgEJAAAAAAAAATEAAAAAAAIBCQAAAAAAAgEtA",
  "granule": "example-granule",
  "line": 100,
  "sample": 200,
  "area_m2": 140625.0,
  "scan_angle": 0.0,
  "vza": 10.0,
  "sza": 35.0,
  "raa": 25.0,
  "glint_angle": 45.0,
  "stratum": "night_forest_nadir_VIIRS_I",
  "T_mir": 300.0,
  "T_tir": 290.0,
  "mir_channel": "I4",
  "r_block": 1.0,
  "r_win": 0.8,
  "s_win": 0.5,
  "h_lev": 0.2,
  "b_hist": 0.1,
  "q_hist": 0.6,
  "n_hist": 20,
  "s_eff": 0.7,
  "z": 4.1,
  "p_value": 0.0001,
  "nfa_pixel": 0.5,
  "nfa_cluster": 0.2,
  "nfa_multi": 0.1,
  "significance": 1.0,
  "decision": "confirmed",
  "frp_mw": 12.5,
  "frp_err_mw": 0.8,
  "frp_toa_mw": 13.1,
  "frp_50_mw": 4.0,
  "flags": 0,
  "event_id": null,
  "source_class": "unknown"
}
```

### Результат с ошибкой

Пример одной ошибки отсутствующего поля `platform`. В результате `record` равен `null`; `errors` может содержать несколько ошибок одной записи.

```json
{
  "valid": false,
  "record": null,
  "errors": [
    {
      "code": "MISSING_REQUIRED_FIELD",
      "name": "Отсутствует обязательное поле",
      "field": "platform",
      "type": "missing",
      "message": "Field required",
      "severity": "error",
      "can_continue": true
    }
  ],
  "warnings": []
}
```

### Отчёт качества

```json
{
  "validation": {
    "valid_count": 1,
    "invalid_count": 1,
    "errors": [
      {
        "code": "VALUE_OUT_OF_RANGE",
        "name": "Значение вне допустимого диапазона",
        "field": "det_id",
        "type": "greater_than_equal",
        "message": "Input should be greater than or equal to 0",
        "severity": "error",
        "can_continue": true
      }
    ],
    "warnings": []
  }
}
```

## 5. JSON Schema

Ниже приведена JSON Schema Draft 2020-12 для трёх моделей. Она сгенерирована Pydantic через `model_json_schema(mode="validation")`; поля `title` и `description` исключены для компактности. Тест `tests/test_json_contract.py` проверяет, что опубликованная схема соответствует моделям. Дополнительные правила времени, геометрии и согласованности результата проверяются валидаторами Pydantic во время выполнения.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "oneOf": [
    {
      "$ref": "#/$defs/DetectionRecord"
    },
    {
      "$ref": "#/$defs/DetectionValidationResult"
    },
    {
      "$ref": "#/$defs/QualityReportData"
    }
  ],
  "$defs": {
    "DetectionRecord": {
      "additionalProperties": false,
      "properties": {
        "det_id": {
          "maximum": 18446744073709551615,
          "minimum": 0,
          "type": "integer"
        },
        "platform": {
          "enum": [
            "NOAA-20",
            "NOAA-21",
            "S-NPP",
            "S3A",
            "S3B",
            "S3C"
          ],
          "type": "string"
        },
        "sensor_view": {
          "enum": [
            "VIIRS_I",
            "VIIRS_M",
            "SLSTR_nadir",
            "SLSTR_oblique"
          ],
          "type": "string"
        },
        "t_utc": {
          "format": "date-time",
          "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d{1,3}0{0,3})?(?:Z|\\+00:00)$",
          "type": "string"
        },
        "lat": {
          "format": "double",
          "type": "number"
        },
        "lon": {
          "format": "double",
          "type": "number"
        },
        "footprint": {
          "contentEncoding": "base64",
          "format": "binary",
          "type": "string"
        },
        "granule": {
          "type": "string"
        },
        "line": {
          "maximum": 2147483647,
          "minimum": -2147483648,
          "type": "integer"
        },
        "sample": {
          "maximum": 2147483647,
          "minimum": -2147483648,
          "type": "integer"
        },
        "area_m2": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "scan_angle": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "vza": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "sza": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "raa": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "glint_angle": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "stratum": {
          "type": "string"
        },
        "T_mir": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "T_tir": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "mir_channel": {
          "enum": [
            "I4",
            "M13",
            "S7",
            "F1"
          ],
          "type": "string"
        },
        "r_block": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "r_win": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "s_win": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "h_lev": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "b_hist": {
          "anyOf": [
            {
              "format": "float",
              "maximum": 3.4028234663852886e+38,
              "minimum": -3.4028234663852886e+38,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "q_hist": {
          "anyOf": [
            {
              "format": "float",
              "maximum": 3.4028234663852886e+38,
              "minimum": -3.4028234663852886e+38,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "n_hist": {
          "minimum": 0,
          "type": "integer"
        },
        "s_eff": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "z": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "p_value": {
          "exclusiveMinimum": 0,
          "format": "float",
          "maximum": 1,
          "type": "number"
        },
        "nfa_pixel": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "nfa_cluster": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "nfa_multi": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "significance": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "decision": {
          "enum": [
            "confirmed",
            "needs_confirmation",
            "rejected"
          ],
          "type": "string"
        },
        "frp_mw": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "frp_err_mw": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "frp_toa_mw": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "frp_50_mw": {
          "format": "float",
          "maximum": 3.4028234663852886e+38,
          "minimum": -3.4028234663852886e+38,
          "type": "number"
        },
        "flags": {
          "maximum": 4294967295,
          "minimum": 0,
          "type": "integer"
        },
        "event_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "source_class": {
          "enum": [
            "vegetation_fire",
            "persistent_industrial",
            "persistent_natural_candidate",
            "recurrent_seasonal",
            "volcano_candidate",
            "unknown"
          ],
          "type": "string"
        }
      },
      "required": [
        "det_id",
        "platform",
        "sensor_view",
        "t_utc",
        "lat",
        "lon",
        "footprint",
        "granule",
        "line",
        "sample",
        "area_m2",
        "scan_angle",
        "vza",
        "sza",
        "raa",
        "glint_angle",
        "stratum",
        "T_mir",
        "T_tir",
        "mir_channel",
        "r_block",
        "r_win",
        "s_win",
        "h_lev",
        "n_hist",
        "s_eff",
        "z",
        "p_value",
        "nfa_pixel",
        "nfa_cluster",
        "nfa_multi",
        "significance",
        "decision",
        "frp_mw",
        "frp_err_mw",
        "frp_toa_mw",
        "frp_50_mw",
        "flags",
        "source_class"
      ],
      "type": "object"
    },
    "ValidationIssue": {
      "additionalProperties": false,
      "properties": {
        "code": {
          "enum": [
            "INVALID_INPUT",
            "MISSING_REQUIRED_FIELD",
            "INVALID_FIELD_TYPE",
            "VALUE_OUT_OF_RANGE",
            "INVALID_FIELD_VALUE",
            "UNEXPECTED_FIELD"
          ],
          "type": "string"
        },
        "name": {
          "type": "string"
        },
        "field": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ]
        },
        "type": {
          "type": "string"
        },
        "message": {
          "type": "string"
        },
        "severity": {
          "enum": [
            "error",
            "warning"
          ],
          "type": "string"
        },
        "can_continue": {
          "type": "boolean"
        }
      },
      "required": [
        "code",
        "name",
        "field",
        "type",
        "message",
        "severity",
        "can_continue"
      ],
      "type": "object"
    },
    "DetectionValidationResult": {
      "additionalProperties": false,
      "properties": {
        "valid": {
          "type": "boolean"
        },
        "record": {
          "anyOf": [
            {
              "$ref": "#/$defs/DetectionRecord"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        },
        "errors": {
          "items": {
            "$ref": "#/$defs/ValidationIssue"
          },
          "type": "array"
        },
        "warnings": {
          "items": {
            "$ref": "#/$defs/ValidationIssue"
          },
          "type": "array"
        }
      },
      "required": [
        "valid"
      ],
      "type": "object"
    },
    "ValidationSummary": {
      "additionalProperties": false,
      "properties": {
        "valid_count": {
          "minimum": 0,
          "type": "integer"
        },
        "invalid_count": {
          "minimum": 0,
          "type": "integer"
        },
        "errors": {
          "items": {
            "$ref": "#/$defs/ValidationIssue"
          },
          "type": "array"
        },
        "warnings": {
          "items": {
            "$ref": "#/$defs/ValidationIssue"
          },
          "type": "array"
        }
      },
      "required": [
        "valid_count",
        "invalid_count"
      ],
      "type": "object"
    },
    "QualityReportData": {
      "additionalProperties": true,
      "properties": {
        "validation": {
          "$ref": "#/$defs/ValidationSummary"
        }
      },
      "required": [
        "validation"
      ],
      "type": "object"
    }
  }
}
```
