"""
Наблюдаемость Telegram-транспорта: классификация ошибок PTB, rolling-счётчики
здоровья и rate limit для transport-предупреждений.

Модуль сознательно не зависит ни от одного другого модуля проекта и работает
только на стандартной библиотеке: он импортируется из точки входа раньше
торгового ядра и не должен тянуть за собой конфигурацию, сеть или биржу.

Состояние процесс-локальное и живёт только в памяти: перезапуск процесса
естественным образом обнуляет health-состояние. Никакой записи в БД или журнал
здесь нет и быть не должно.

Классификация fail-closed: TRANSPORT возвращается только когда исключение
доказанно относится к известному transport-классу или несёт доказанный
HTTP-статус шлюза. Всё остальное — LOGIC. Текст исключения никогда не является
доказательством: строка «network», «timeout» или «gateway» сама по себе ничего
не доказывает.
"""

import importlib
import inspect
import logging
import time
from collections import deque
from functools import wraps

# ---------------------------------------------------------------------------
# Константы контракта
# ---------------------------------------------------------------------------

TRANSPORT = "TRANSPORT"
LOGIC = "LOGIC"

# Окно rolling-счётчиков: ровно 60 минут наблюдений, а не lifetime-счётчик.
WINDOW_SEC = 3600

# Порог деградации обработки команд.
DEGRADED_THRESHOLD = 5

# Кулдаун лога transport-предупреждений. Он намеренно отделён от кулдауна
# Telegram-нотификатора: подавление лога не должно зависеть от алертов и
# наоборот.
TRANSPORT_LOG_COOLDOWN_SEC = 1800

# Идентичность подавления transport-предупреждений.
TRANSPORT_LOG_KEY = "ptb_polling_neterr"

# Доказанные статусы шлюза Telegram.
GATEWAY_STATUS_CODES = frozenset({502, 503, 504})

# Ограничение обхода цепочки cause/context — защита от длинных и циклических
# цепочек.
_MAX_CHAIN_DEPTH = 10

# Доказанные transport-классы. Отсутствие библиотеки или подменённый в тестах
# модуль не должны ломать классификацию: непроверяемый кандидат просто
# игнорируется.
_TRANSPORT_TYPES = (
    ("telegram.error", ("NetworkError",)),
    ("httpx", ("ReadError", "ConnectError", "ReadTimeout", "PoolTimeout")),
    ("httpcore", ("ReadError", "ConnectError", "ReadTimeout", "PoolTimeout")),
)

# ---------------------------------------------------------------------------
# Состояние процесса (только в памяти)
# ---------------------------------------------------------------------------

_polling_errors: deque = deque()
_commands_processed: deque = deque()
_commands_failed: deque = deque()

_consecutive_handler_failures = 0

_transport_log = {"last_ts": None, "suppressed": 0}


def _now() -> float:
    """Монотонное время наблюдений; тесты подменяют именно эту функцию."""
    return time.monotonic()


def reset_health_state() -> None:
    """Сбрасывает всё process-local состояние (используется тестами)."""
    global _consecutive_handler_failures
    _polling_errors.clear()
    _commands_processed.clear()
    _commands_failed.clear()
    _consecutive_handler_failures = 0
    _transport_log["last_ts"] = None
    _transport_log["suppressed"] = 0


# ---------------------------------------------------------------------------
# Классификация ошибок PTB
# ---------------------------------------------------------------------------

def _load_types(spec) -> list:
    """Возвращает реально существующие классы исключений из *spec*.

    Модуль может отсутствовать или быть подменён заглушкой: кандидат
    принимается только если это настоящий класс исключения.
    """
    found = []
    for module_name, attribute_names in spec:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for name in attribute_names:
            candidate = getattr(module, name, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseException):
                found.append(candidate)
    return found


def _has_proven_gateway_status(exc) -> bool:
    """True только при доказанном целом HTTP-статусе шлюза 502/503/504."""
    for holder in (exc, getattr(exc, "response", None)):
        if holder is None:
            continue
        status = getattr(holder, "status_code", None)
        # bool — подкласс int, но не доказательство статуса.
        if isinstance(status, bool) or not isinstance(status, int):
            continue
        if status in GATEWAY_STATUS_CODES:
            return True
    return False


def _is_proven_transport(exc) -> bool:
    """True только для доказанного transport-звена цепочки исключений."""
    for known in _load_types(_TRANSPORT_TYPES):
        if isinstance(exc, known):
            return True
    return _has_proven_gateway_status(exc)


def _iter_chain(exc):
    """Обходит исключение и его доказанную цепочку cause/context."""
    seen = set()
    current = exc
    depth = 0
    while isinstance(current, BaseException) and depth < _MAX_CHAIN_DEPTH:
        if id(current) in seen:
            return
        seen.add(id(current))
        yield current
        following = current.__cause__
        if following is None:
            following = current.__context__
        current = following
        depth += 1


def classify_ptb_error(exc) -> str:
    """Классифицирует исключение PTB как TRANSPORT или LOGIC.

    TRANSPORT — только доказанный transport-класс или доказанный статус шлюза,
    в том числе внутри цепочки cause/context, которой PTB оборачивает исходную
    ошибку канала. Всё недоказанное, включая не-исключения, — LOGIC.
    """
    for link in _iter_chain(exc):
        if _is_proven_transport(link):
            return TRANSPORT
    return LOGIC


def is_updater_polling_traceback(exc) -> bool:
    """Проверяет происхождение traceback из getUpdates loop PTB."""
    traceback = getattr(exc, "__traceback__", None)
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__", "")
        filename = frame.f_code.co_filename.replace("\\", "/")
        if (
            (module == "telegram.ext._updater" or filename.endswith("/telegram/ext/_updater.py"))
            and frame.f_code.co_name == "polling_action_cb"
        ):
            return True
        traceback = traceback.tb_next
    return False


def is_polling_transport_error(update, context, exc) -> bool:
    """True только для доказанной transport-ошибки цикла getUpdates.

    Единственная точка решения: обновления, задачи планировщика и корутины
    приложения сюда не попадают, а сама ошибка обязана быть доказанным
    transport-классом из кадра polling-цикла.
    """
    if update is not None:
        return False
    if getattr(context, "job", None) is not None:
        return False
    if getattr(context, "coroutine", None) is not None:
        return False
    if classify_ptb_error(exc) != TRANSPORT:
        return False
    return is_updater_polling_traceback(exc)


# ---------------------------------------------------------------------------
# Rolling-счётчики за последние 60 минут
# ---------------------------------------------------------------------------

def _prune(bucket: deque, now: float) -> None:
    """Удаляет наблюдения старше окна: счётчик обязан быть честно rolling."""
    cutoff = now - WINDOW_SEC
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()


def _observe(bucket: deque) -> None:
    now = _now()
    _prune(bucket, now)
    bucket.append(now)


def record_polling_error() -> None:
    """Учитывает фактическую transport-ошибку polling.

    Вызывается на каждой ошибке, независимо от подавления лога: дедупликация
    предупреждений не имеет права прятать счётчик.
    """
    _observe(_polling_errors)


def record_command_started() -> None:
    """Учитывает начало фактической обработки пользовательской команды."""
    _observe(_commands_processed)


def record_command_failed() -> int:
    """Учитывает команду, завершившуюся необработанным исключением."""
    global _consecutive_handler_failures
    _observe(_commands_failed)
    _consecutive_handler_failures += 1
    return _consecutive_handler_failures


def record_command_success() -> None:
    """Сбрасывает счётчик подряд идущих сбоев после доказанного успеха."""
    global _consecutive_handler_failures
    _consecutive_handler_failures = 0


def get_consecutive_handler_failures() -> int:
    """Текущее число подряд идущих сбоев обработки команд."""
    return _consecutive_handler_failures


def is_degraded() -> bool:
    """True, когда обработка команд доказанно деградировала."""
    return _consecutive_handler_failures >= DEGRADED_THRESHOLD


def get_health_snapshot() -> dict:
    """Снимок health-состояния: только счётчики, без Update, контекста и секретов."""
    now = _now()
    _prune(_polling_errors, now)
    _prune(_commands_processed, now)
    _prune(_commands_failed, now)
    return {
        "polling_errors_last_hour": len(_polling_errors),
        "commands_processed_last_hour": len(_commands_processed),
        "commands_failed_last_hour": len(_commands_failed),
        "consecutive_handler_failures": _consecutive_handler_failures,
        "degraded": is_degraded(),
        "window_minutes": WINDOW_SEC // 60,
    }


# ---------------------------------------------------------------------------
# Rate limit лога transport-предупреждений
# ---------------------------------------------------------------------------

def allow_transport_warning() -> tuple:
    """Решает, можно ли писать очередное transport-предупреждение.

    Возвращает ``(разрешено, подавлено_с_прошлого_раза)``. Подавление никогда
    не становится вечным: после истечения кулдауна следующее предупреждение
    снова разрешено и сообщает, сколько наблюдений было пропущено.
    """
    now = _now()
    last = _transport_log["last_ts"]
    if last is not None and (now - last) < TRANSPORT_LOG_COOLDOWN_SEC:
        _transport_log["suppressed"] += 1
        return False, 0
    suppressed = _transport_log["suppressed"]
    _transport_log["last_ts"] = now
    _transport_log["suppressed"] = 0
    return True, suppressed


def log_polling_transport_error(exc) -> bool:
    """Полная обработка подтверждённой transport-ошибки polling.

    Счётчик увеличивается всегда, лог — не чаще, чем разрешает rate limit.
    Возвращает True, если предупреждение было записано. Уровень намеренно
    WARNING и без traceback: временный ретрай канала не является аварией.
    """
    record_polling_error()
    allowed, suppressed = allow_transport_warning()
    if not allowed:
        return False
    if suppressed:
        logging.warning(
            "PTB polling transport error: %s (подавлено с прошлого раза: %d)",
            exc,
            suppressed,
        )
    else:
        logging.warning("PTB polling transport error: %s", exc)
    return True


# ---------------------------------------------------------------------------
# Инструментирование команд
# ---------------------------------------------------------------------------

def instrument_command(callback, on_degraded=None):
    """Оборачивает command-callback учётом факта обработки.

    Обёртка прозрачна: она не меняет аргументы, возвращаемое значение и не
    проглатывает исключения — исключение учитывается и пробрасывается дальше,
    в штатный error handler PTB. Счётчики живут только здесь, поэтому одно и
    то же исключение не может быть учтено дважды.

    ``on_degraded`` вызывается ровно в момент доказанного достижения порога
    деградации и получает только контекст, исключение и число сбоев — ни
    Update, ни traceback. Его собственная ошибка логируется и никогда не
    подменяет исходное исключение обработчика.

    ``BaseException`` (например, отмена задачи) намеренно не считается сбоем
    обработки: это остановка, а не деградация.
    """

    @wraps(callback)
    async def instrumented(update, context):
        record_command_started()
        try:
            result = callback(update, context)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            failures = record_command_failed()
            if on_degraded is not None and failures >= DEGRADED_THRESHOLD:
                try:
                    await on_degraded(context, exc, failures)
                except Exception:
                    logging.exception(
                        "Не удалось отправить alert о деградации обработки команд"
                    )
            raise
        record_command_success()
        return result

    return instrumented
