"""
Адрес доставки автоматических DAILY/WEEKLY Telegram-отчётов.

Коснулся только исходящей доставки двух плановых отчётов: утреннего баланса
(daily_balance_job) и недельного отчёта по источникам (weekly_source_report_job).
Ручной /report здесь не участвует вовсе — он остаётся интерактивным ответом
личного бота владельца.

Конфигурация:

    TELEGRAM_REPORT_DESTINATION = owner | custom   (по умолчанию owner)
    TELEGRAM_REPORT_CHAT_ID     = <ненулевое целое>  (обязателен при custom)
    TELEGRAM_REPORT_THREAD_ID   = <положительное целое> (необязателен, топик)

Контракт fail-closed только для отчётов:

- ``owner`` — существующее поведение: личный чат ALLOWED_TELEGRAM_ID;
- ``custom`` — ТОЛЬКО указанный чат/топик, без дублирующей копии владельцу;
- любое неверное значение (неизвестный enum, пустой/нецелый chat id,
  неверный thread id) делает автоматические отчёты недоступными: ясная
  ошибка в лог и ``None``. Молчаливый откат на владельца запрещён, падать
  торговое приложение из-за опционального адреса отчётов тоже запрещено —
  исключений модуль не поднимает.

Авторизация ingress не расширяется: custom-чат является только адресатом
исходящих отчётов и не даёт сообщениям из него никакой торговой власти.
"""

import logging
from dataclasses import dataclass

from core.config import (
    ALLOWED_ID,
    TELEGRAM_REPORT_CHAT_ID,
    TELEGRAM_REPORT_DESTINATION,
    TELEGRAM_REPORT_THREAD_ID,
)

# Допустимые значения TELEGRAM_REPORT_DESTINATION.
DESTINATION_OWNER = "owner"
DESTINATION_CUSTOM = "custom"


@dataclass(frozen=True)
class ScheduledReportDestination:
    """Неизменяемый адрес доставки одного автоматического отчёта."""

    chat_id: int
    thread_id: int | None = None

    @property
    def send_kwargs(self) -> dict:
        """Параметры bot.send_message: chat_id и, для топика, message_thread_id."""
        kwargs: dict = {"chat_id": self.chat_id}
        if self.thread_id is not None:
            kwargs["message_thread_id"] = self.thread_id
        return kwargs


def _parse_strict_int(raw) -> int | None:
    """Строгое целое из строки: опциональный знак и только ASCII-цифры.

    Пустая строка, пробелы, float-запись ("1.0"), экспонента, нестроковое
    значение и не-ASCII цифры доказанным целым не являются.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    digits = text[1:] if text[0] in "+-" else text
    if not digits or not all(ch in "0123456789" for ch in digits):
        return None
    return int(text)


def _unavailable(reason: str) -> None:
    """Ясная ошибка конфигурации: отчёты недоступны, отката на владельца нет."""
    logging.error(
        "Scheduled reports: %s — автоматические DAILY/WEEKLY отчёты не "
        "отправляются; отката на чат владельца нет (fail-closed только для "
        "отчётов, торговый бот продолжает работу)",
        reason,
    )


def resolve_scheduled_report_destination() -> ScheduledReportDestination | None:
    """Определяет адрес доставки автоматических DAILY/WEEKLY отчётов.

    Чистая функция от значений конфигурации: сеть и записи отсутствуют.
    Возвращает :class:`ScheduledReportDestination` либо ``None`` — «не
    отправлять ничего». ``None`` не означает «отправить владельцу»: молчаливый
    fallback запрещён задачей. Исключений не поднимает — неверная настройка
    опционального адреса отчётов не имеет права уронить приложение.
    """
    raw_mode = TELEGRAM_REPORT_DESTINATION
    if raw_mode is None:
        mode = DESTINATION_OWNER
    elif isinstance(raw_mode, str):
        mode = raw_mode.strip() or DESTINATION_OWNER
    else:
        mode = None

    if mode == DESTINATION_OWNER:
        owner_chat_id = _parse_strict_int(ALLOWED_ID)
        if owner_chat_id is None or owner_chat_id == 0:
            _unavailable(
                f"ALLOWED_TELEGRAM_ID={ALLOWED_ID!r} не является ненулевым целым"
            )
            return None
        return ScheduledReportDestination(chat_id=owner_chat_id)

    if mode == DESTINATION_CUSTOM:
        chat_id = _parse_strict_int(TELEGRAM_REPORT_CHAT_ID)
        # Отрицательные ID допустимы: custom-адрес — общий чат Telegram,
        # а не только группа. Запрещены только ноль и нецелое значение.
        if chat_id is None or chat_id == 0:
            _unavailable(
                "TELEGRAM_REPORT_DESTINATION=custom требует ненулевой целый "
                f"TELEGRAM_REPORT_CHAT_ID (получено {TELEGRAM_REPORT_CHAT_ID!r})"
            )
            return None
        thread_id = None
        raw_thread = TELEGRAM_REPORT_THREAD_ID
        if isinstance(raw_thread, str) and raw_thread.strip():
            thread_id = _parse_strict_int(raw_thread)
            if thread_id is None or thread_id <= 0:
                _unavailable(
                    "TELEGRAM_REPORT_THREAD_ID="
                    f"{TELEGRAM_REPORT_THREAD_ID!r} не является положительным целым"
                )
                return None
        return ScheduledReportDestination(chat_id=chat_id, thread_id=thread_id)

    _unavailable(
        f"TELEGRAM_REPORT_DESTINATION={raw_mode!r}: ожидается "
        f"'{DESTINATION_OWNER}' или '{DESTINATION_CUSTOM}'"
    )
    return None
