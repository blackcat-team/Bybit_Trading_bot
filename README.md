# 📈 Bybit Telegram Trading Bot
![Visitor Badge](https://visitor-badge.laobi.icu/badge?page_id=blackcat-team.Bybit_Trading_bot)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)
![Bybit](https://img.shields.io/badge/Bybit-API-orange)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Active-success)

### Описание
Модульный торговый бот для **Bybit (Linear USDT)** с управлением через Telegram.
Бот сфокусирован на строгом риск-менеджменте, математическом ожидании и исключении эмоциональных ошибок. Поддерживает парсинг сигналов из текста, ручную торговлю и полностью автоматическое сопровождение сделок.

---

## 🚀 Ключевые возможности

### 1. Риск-менеджмент (R-System)
* **Фиксированный риск:** Рассчитывает объем позиции (`qty`) так, чтобы при стопе потерять ровно заданную сумму (например, $50).
* **Защита:** Блокирует сделки с нулевым объемом, защищает от "перепутанных" кнопок Long/Short и контролирует доступную маржу.

### 2. Умное управление позицией (Smart Logic)
* **📉 Ступенчатый Безубыток:**
    * Прибыль ≥ **1R**: Сокращает риск на 70% (подтягивает SL в зону -0.3R).
    * Прибыль ≥ **2R**: Переводит в полный БУ (Вход + 0.05R для покрытия комиссий).
* **💰 Smart Take Profits:** Автоматическая расстановка лесенки тейков по стратегии "30/30/40":
    * 30% позиции закрывается на **1R**.
    * 30% позиции закрывается на **2R**.
    * 40% позиции закрывается на **3R**.
* **⏳ Тайм-менеджмент (Time-Based):**
    * **5 дней:** Предупреждение, если сделка "зависла" (прибыль <1R).
    * **7 дней:** Критический алерт с требованием закрыть сделку немедленно.

### 3. Интерфейс и Отчеты
* **Парсинг сигналов:** Распознает текстовые сигналы (`COIN ENTRY STOP`) и хештеги источников.
* **Интерактив:** Кнопки "Вход по рынку", "Авто-Тейки", "Аварийное закрытие".
* **Отчетность:** Генерация CSV-отчетов и статистика PnL/Winrate за месяц командой `/report`.

---

## 🛠 Техническая часть
* **Язык:** Python 3.10+
* **API:** `pybit` (Bybit V5 Unified/Contract)
* **Bot:** `python-telegram-bot` (Async)
* **Deployment:** Поддержка `systemd` для работы 24/7.

---

## ⚙️ Установка и Настройка

### 1. Подготовка сервера (VPS)
Вам понадобится сервер с Ubuntu 20.04/22.04.

```bash
# Клонируем репозиторий
git clone [https://github.com/blackcat-team/Bybit_Trading_bot.git](https://github.com/blackcat-team/Bybit_Trading_bot.git)
cd Bybit_Trading_bot

# Создаем виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate

# Устанавливаем зависимости
pip install -r requirements.txt
```

### 2. Создание Telegram бота
1.  Напишите **@BotFather** в Telegram.
2.  Отправьте команду `/newbot`.
3.  Скопируйте **Token**.
4.  Узнайте свой цифровой ID через бота **@userinfobot**.

### 3. Настройка Config
Создайте `.env` в корне проекта и заполните переменные:

```env
# ── Обязательные ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN=ВАШ_ТОКЕН
ALLOWED_TELEGRAM_ID=ВАШ_TELEGRAM_ID
BYBIT_API_KEY=ВАШ_API_KEY
BYBIT_API_SECRET=ВАШ_API_SECRET

# ── Основные параметры ───────────────────────────────────────────────────────
USER_RISK_USD=50          # Риск на сделку в $
IS_DEMO=False             # True = демо-счёт Bybit

# ── Защита маржи ─────────────────────────────────────────────────────────────
MARGIN_BUFFER_USD=1.0     # Абсолютный запас маржи ($)
MARGIN_BUFFER_PCT=0.03    # Относительный запас (3%)

# ── Лимит тепла (совокупный риск-под-стопом) ─────────────────────────────────
# 0 = выключено (по умолчанию)
MAX_TOTAL_HEAT_USDT=0
# reject = отклонить сделку; queue = поставить в очередь
HEAT_ACTION=reject
HEAT_QUEUE_TTL_MIN=30     # Время жизни очереди (мин)

# ── Политика конфликта сигналов ───────────────────────────────────────────────
# ignore = игнорировать (по умолчанию); add_if_allowed = разрешить добавление
CONFLICT_POLICY_SAME_DIR=ignore
SOURCE_ALLOW_ADD=0        # 1 = разрешить добавление к позиции (если add_if_allowed)

# ── Автокарантин источников ───────────────────────────────────────────────────
# 0 = выключено (по умолчанию)
QUARANTINE_LOSS_STREAK=0        # Карантин после N подряд убыточных сделок
QUARANTINE_DAILY_PNL_USDT=0     # Карантин, если дневной PnL < N (отриц. = разрешить убыток)
QUARANTINE_WEEKLY_PNL_USDT=0    # То же самое, недельное окно

# ── Подтверждение маркет-входа ────────────────────────────────────────────────
# 0 = существующее поведение "GO MARKET" (по умолчанию)
# 1 = сначала показать превью; исполнение — только после нажатия CONFIRM
REQUIRE_MARKET_CONFIRM=0
MARKET_PREVIEW_TTL_SEC=300      # Время жизни кнопки CONFIRM (сек)
```

### 4. Подготовка файлов данных
Бот хранит рабочие файлы (настройки, журнал рисков, источники сигналов) в папке `data/`.
При первом запуске она создается автоматически. Чтобы начать с шаблонов:

```bash
cd data
cp settings.example.json settings.json
cp risk_data.example.json risk_data.json
cp journal_comments.example.json journal_comments.json
cp sources_log.example.json sources_log.json
```

| Файл | Описание |
| :--- | :--- |
| `data/settings.json` | Вкл/выкл торговли, глобальный риск |
| `data/risk_data.json` | Индивидуальный риск по монетам |
| `data/journal_comments.json` | Заметки `/note` |
| `data/sources_log.json` | Лог источников сигналов |
| `data/trade_journal.jsonl` | Торговый журнал (JSONL, append-only) |
| `data/disabled_sources.json` | Источники в карантине |
| `data/heat_queue.json` | Очередь сделок (ожидают снижения тепла) |

---

## 🎮 Команды бота

| Команда | Описание |
| :--- | :--- |
| `/start` | Запуск торговли (снятие паузы). |
| `/stop` | ⛔️ Пауза (запрет новых сделок). |
| `/status` | Снимок состояния: торговля, дневной PnL, позиции, тепло, алерты, карантин. |
| `/risk 50` | Установить риск $50 на сделку. |
| `/pos` | Показать открытые позиции и PnL. |
| `/orders` | Показать активные лимитные ордера. |
| `/report` | Прислать отчет за день. |
| `/note BTC Текст` | Записать заметку к монете в торговый дневник. |

---

## 🖥 Запуск 24/7 (Systemd)

Чтобы бот работал в фоне и перезапускался при ошибках.
Готовый файл службы находится в `deploy/bybit-bot.service` — скопируйте и отредактируйте пути.

**1. Создайте пользователя и директорию (не root):**
```bash
sudo useradd -r -s /sbin/nologin botuser
sudo mkdir -p /opt/bybit-bot
sudo chown -R botuser:botuser /opt/bybit-bot
# Скопируйте файлы проекта в /opt/bybit-bot и создайте .venv
```

**2. Скопируйте файл службы:**
```bash
sudo cp deploy/bybit-bot.service /etc/systemd/system/bybit-bot.service
# Отредактируйте WorkingDirectory и пути если нужно:
# sudo nano /etc/systemd/system/bybit-bot.service
```

**3. Запустите:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable bybit-bot
sudo systemctl start bybit-bot
```
**Управление:**

Статус:
```bash
systemctl status bybitbot
```
Логи: 
```bash
journalctl -u bybitbot -f
```
Рестарт: 
```bash
systemctl restart bybitbot
```
Остановка: 
```bash
systemctl stop  bybitbot
```
Обновление бота: 
```bash
sudo systemctl stop bybitbot

cd Bybit_Trading_bot
git pull
source .venv/bin/activate
pip install -r requirements.txt

sudo systemctl start bybitbot
sudo journalctl -u bybitbot -n 80 --no-pager
```

---

## ✅ Production Checklist

Before going live, verify the following:

**API & auth**
- [ ] `BYBIT_API_KEY` / `BYBIT_API_SECRET` — created with Trade-only permission, IP whitelist set
- [ ] `ALLOWED_TELEGRAM_ID` — only your personal ID (never a group/channel)
- [ ] `IS_DEMO=False` in `.env`

**Risk controls**
- [ ] `USER_RISK_USD` — set to a value you are comfortable losing per trade
- [ ] `DAILY_LOSS_LIMIT` in `core/config.py` — set to your prop-firm or personal limit (default −50 USD)
- [ ] `MAX_TOTAL_HEAT_USDT` — set to your total open-risk ceiling (0 = disabled)
- [ ] `REQUIRE_MARKET_CONFIRM=1` — recommended for live trading (shows preview before execution)

**Signal safety**
- [ ] `CONFLICT_POLICY_SAME_DIR=ignore` — prevents duplicate entries by default
- [ ] `QUARANTINE_LOSS_STREAK` — consider 3–5 to auto-disable poorly-performing sources
- [ ] Review sources in `/status` before first signal

**Infrastructure**
- [ ] Bot runs as non-root user with `systemd` service (`deploy/bybit-bot.service`)
- [ ] `data/` directory exists and is writable by the bot user
- [ ] Test on Bybit testnet (`IS_DEMO=True`) before switching to live

**Post-launch**
- [ ] Send `/start` to enable signal processing
- [ ] Confirm `/status` shows correct balance and no errors
- [ ] Place one small test trade end-to-end and verify TP/SL placement
