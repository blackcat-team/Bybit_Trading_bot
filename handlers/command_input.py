"""
Разговорный ввод аргументов команд Telegram через нативный ForceReply.

Нативное меню команд Telegram отправляет команду сразу, без аргументов, поэтому
команды, которым аргумент нужен (/risk, /price, /timeline, /note и «другой
месяц» для /report), в меню неудобны. Этот модуль добавляет единый безопасный
поток: команда без достаточных аргументов присылает подсказку с ForceReply, а
оператор отвечает значением прямо в поле ответа.

Механизм намеренно узкий и НЕ образует вторую плоскость управления:

* ожидание привязано к авторизованному оператору (:data:`ALLOWED_ID`);
* фиксируется ТОЧНАЯ идентичность сообщения-подсказки бота — пара
  (chat_id, message_id) — и вид ввода; ``message_id`` в Telegram уникален лишь
  в пределах чата, поэтому одного номера недостаточно;
* потребляется ТОЛЬКО прямой reply на это активное сообщение-подсказку в том же
  чате: совпадение только по номеру message_id из другого чата отвергается;
* любое иное сообщение и любой reply на другое сообщение проходят дальше в
  существующие обработчики без изменений — обычный текст и торговые сигналы
  никогда не превращаются в значение команды;
* ответ потребляется ровно один раз, после чего ожидание снимается;
* устаревший (за пределом TTL) ответ на активную подсказку завершается
  безопасно и в парсер сигналов не попадает;
* новая подсказка того же оператора перекрывает прежнюю — активной остаётся
  ровно одна; reply на прежнюю (уже не отслеживаемую) подсказку не потребляется;
* состояние живёт только в памяти процесса и не переживает рестарт.

Доменная логика не дублируется: потреблённый ответ разбивается на токены по
пробелам ровно как это делает :class:`telegram.ext.CommandHandler`, и передаётся
тому же авторитетному обработчику команды с восстановленным ``context.args``.
"""

import logging
import time

from telegram import ForceReply, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from core.config import ALLOWED_ID


# --- Виды ожидаемого ввода ---
RISK = "risk"
PRICE = "price"
TIMELINE = "timeline"
NOTE = "note"
REPORT_MONTH = "report_month"

_KINDS = frozenset({RISK, PRICE, TIMELINE, NOTE, REPORT_MONTH})

# Ожидание живёт ограниченное время: reply на давно заброшенную подсказку
# завершается безопасно, а не исполняется внезапно спустя часы.
PENDING_INPUT_TTL_SEC = 300

# Ожидающий ввод: user_id -> {"kind", "prompt_chat_id", "prompt_message_id",
# "created_at"}. Точная идентичность подсказки — пара (chat_id, message_id).
# ТОЛЬКО в памяти процесса; при рестарте состояние теряется намеренно —
# незавершённый разговорный ввод не должен «оживать» после перезапуска.
_PENDING: dict = {}


def _now() -> float:
    return time.time()


def _is_fresh(created_at: float) -> bool:
    return _now() - created_at <= PENDING_INPUT_TTL_SEC


def _clear(user_id: str) -> None:
    _PENDING.pop(user_id, None)


def _chat_id_of(message):
    """Идентичность чата сообщения Telegram: ``chat_id`` (== ``chat.id``) либо None.

    ``message_id`` уникален только в пределах чата, поэтому точная идентичность
    подсказки — пара (chat_id, message_id). ``None`` означает, что идентичность
    чата не доказана: совпадение подтвердить нельзя, и ответ не потребляется
    (fail-closed).
    """
    if message is None:
        return None
    chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        return chat_id
    chat = getattr(message, "chat", None)
    return getattr(chat, "id", None)


def _warning(text: str, action: str) -> str:
    """Строит стандартное предупреждение проекта (ленивый импорт UI-хелперов).

    UI-модуль импортируется внутри функции, чтобы верхний уровень модуля зависел
    только от легко изолируемых зависимостей (telegram, core.config) — это
    сохраняет состояние ``_PENDING`` тривиально тестируемым и не тянет UI в
    цепочку импортов механизма.
    """
    from handlers.ui import format_warning_message
    return format_warning_message([text], action=action)


def _resolve_handler(kind: str):
    """Возвращает авторитетный обработчик команды для вида ввода либо None.

    Ленивая привязка внутри функции разрывает цикл импортов: доменные модули
    импортируют :func:`request_input` отсюда, а их обработчики попадают сюда
    только в момент потребления ответа. Обработчик берётся из его домашнего
    модуля как текущий атрибут, поэтому остаётся ровно той же авторитетной
    точкой, что и прямая команда — параллельной реализации не возникает.
    """
    if kind == RISK:
        from handlers.commands import set_risk_command
        return set_risk_command
    if kind == NOTE:
        from handlers.commands import add_note_handler
        return add_note_handler
    if kind == PRICE:
        from handlers.price import price_command
        return price_command
    if kind == TIMELINE:
        from handlers.timeline import timeline_command
        return timeline_command
    if kind == REPORT_MONTH:
        from handlers.reporting import send_report
        return send_report
    return None


async def request_input(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        kind: str, prompt_text: str, placeholder: str) -> None:
    """Присылает подсказку с ForceReply и запоминает ожидание для оператора.

    Точная идентичность подсказки — пара (``chat_id``, ``message_id``)
    отправленного сообщения: потреблён будет только прямой reply именно на него
    и именно в его чате. Новая подсказка того же оператора перекрывает прежнюю.
    Только авторизованный оператор получает подсказку и ожидание.
    """
    if kind not in _KINDS:
        return
    user = update.effective_user
    if user is None or str(user.id) != ALLOWED_ID:
        # Разговорный ввод не расширяет авторизацию: не-владелец не получает ни
        # подсказки, ни ожидания.
        return
    user_id = str(user.id)

    force_reply = ForceReply(input_field_placeholder=placeholder)
    # Реальный Update всегда имеет effective_message; для команды это то же
    # сообщение, что update.message. Fallback на update.message сохраняет
    # совместимость с уже существующими обработчиками, которые отвечают через
    # update.message.
    msg_obj = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if msg_obj is not None:
        sent = await msg_obj.reply_text(
            prompt_text, parse_mode="HTML", reply_markup=force_reply
        )
    else:
        sent = await context.bot.send_message(
            ALLOWED_ID, prompt_text, parse_mode="HTML", reply_markup=force_reply
        )

    prompt_id = getattr(sent, "message_id", None)
    prompt_chat_id = _chat_id_of(sent)
    if prompt_id is None or prompt_chat_id is None:
        # Без точной идентичности подсказки (chat_id + message_id) нельзя
        # доказать, что ответ относится именно к ней — ожидание не создаётся
        # (fail-closed), прежнее снимается.
        _clear(user_id)
        return

    _PENDING[user_id] = {
        "kind": kind,
        "prompt_chat_id": prompt_chat_id,
        "prompt_message_id": prompt_id,
        "created_at": _now(),
    }
    logging.info(
        "command-input: подсказка '%s' оператору, chat=%s prompt_msg=%s",
        kind, prompt_chat_id, prompt_id,
    )


async def handle_command_reply(update: Update,
                               context: ContextTypes.DEFAULT_TYPE) -> None:
    """Потребляет ТОЛЬКО прямой reply оператора на активную подсказку.

    Регистрируется группой раньше и обработчика защиты позиции, и парсера
    сигналов. Для любого сообщения без совпадающего активного ожидания ничего
    не делает и молча пропускает его дальше: обычный текст, торговые сигналы и
    ввод защиты позиции продолжают идти в существующие обработчики без
    изменений.

    Совпавший reply потребляется ровно один раз. После потребления обработка
    останавливается (:class:`telegram.ext.ApplicationHandlerStop`) — потреблённый
    ответ никогда не достигает парсера сигналов, даже если доменный обработчик
    завершится ошибкой.
    """
    user = update.effective_user
    if user is None or str(user.id) != ALLOWED_ID:
        # Не-владелец не может получить власть команды через reply-поток.
        return
    user_id = str(user.id)

    pending = _PENDING.get(user_id)
    if pending is None:
        return

    msg_obj = update.effective_message
    if msg_obj is None:
        return

    reply_to = getattr(msg_obj, "reply_to_message", None)
    reply_chat_id = _chat_id_of(msg_obj)
    if (reply_to is None
            or getattr(reply_to, "message_id", None) != pending["prompt_message_id"]
            or reply_chat_id != pending["prompt_chat_id"]):
        # Не прямой reply на активную подсказку в ЕЁ чате: обычное сообщение,
        # сигнал, reply на прежнюю/перекрытую подсказку либо совпадение только по
        # номеру message_id из другого чата. Ожидание не трогаем, сообщение идёт
        # дальше в существующие обработчики.
        return

    # С этого момента ответ доказанно принадлежит command-input UX: правильный
    # оператор, правильный чат подсказки и правильный message_id. Он ОБЯЗАН быть
    # остановлен от дальнейшего распространения по группам PTB при любом исходе
    # (валидный/невалидный/пустой ввод, устаревшая подсказка, ошибка доменного
    # обработчика, сбой доставки уведомления). Гарантия обеспечивается тем, что
    # ApplicationHandlerStop поднимается в finally: даже если любой await ниже
    # бросит исключение, оно логируется, а stop всё равно происходит.
    #
    # ApplicationHandlerStop поднимается ТОЛЬКО в finally и внутри try не
    # возникает, поэтому ``except Exception`` его не перехватывает.
    try:
        if not _is_fresh(pending["created_at"]):
            _clear(user_id)
            await msg_obj.reply_text(
                _warning(
                    "Подсказка ввода устарела: значение не принято.",
                    "повторите команду, чтобы получить новую подсказку",
                ),
                parse_mode="HTML",
            )
            return

        raw = msg_obj.text or msg_obj.caption
        kind = pending["kind"]
        handler = _resolve_handler(kind)
        # One-shot: ожидание снимается до исполнения, чтобы повтор не завис.
        _clear(user_id)

        if handler is None or raw is None or not str(raw).strip():
            await msg_obj.reply_text(
                _warning(
                    "Пустой ответ: значение не принято.",
                    "повторите команду и отправьте значение в ответ на подсказку",
                ),
                parse_mode="HTML",
            )
            return

        # Токенизация ровно как у CommandHandler (разбиение по пробелам):
        # доменный обработчик получает те же context.args, что и при прямой
        # команде, поэтому его валидация и запись переиспользуются без
        # дублирования.
        context.args = str(raw).split()
        logging.info("command-input: потреблён reply '%s' -> %s токен(ов)",
                     kind, len(context.args))
        await handler(update, context)
    except Exception:
        # Ответ уже авторитетно потреблён. Любой сбой ниже (доменный обработчик
        # ИЛИ доставка уведомления/предупреждения) не должен позволить значению
        # провалиться в парсер сигналов и стать сделкой — ошибка логируется, а
        # остановка всё равно гарантируется в finally (fail-closed).
        logging.exception(
            "command-input: сбой при обработке потреблённого reply оператора",
        )
    finally:
        # Совпавший ответ никогда не достигает групп -1/0 — при любом исходе.
        raise ApplicationHandlerStop
