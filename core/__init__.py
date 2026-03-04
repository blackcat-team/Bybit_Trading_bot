"""
Пакет core — конфигурация, база данных, торговое ядро.

Содержит: config.py (env-переменные), database.py (JSON-хранилище),
trading_core.py (сессия Bybit, TP-лестница), bybit_call.py (async-обёртка),
notifier.py (алерты), heat.py (контроль риска), conflict.py (разрешение
конфликтов сигналов), journal.py (торговый журнал + карантин).
"""
