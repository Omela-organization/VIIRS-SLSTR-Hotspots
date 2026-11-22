"""JSON-логирование через structlog (НФТ-08)."""
import logging
import sys

try:
    import structlog
    _HAS_STRUCTLOG = True
except ImportError:
    _HAS_STRUCTLOG = False


def setup_logging(level: str = "INFO") -> None:
    """Инициализирует логирование для всего приложения."""
    if _HAS_STRUCTLOG:
        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, level.upper())
            ),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
            cache_logger_on_first_use=True,
        )
    else:
        # Фолбэк на стандартный logging, если structlog не установлен
        logging.basicConfig(
            level=getattr(logging, level.upper()),
            format="%(asctime)s %(levelname)s %(message)s",
            stream=sys.stderr,
        )


def get_logger(name: str = "viirs_slstr_hotspots"):
    """Возвращает настроенный логгер для использования в модулях."""
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)