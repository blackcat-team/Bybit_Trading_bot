<div align="center">

# 📈 Bybit Telegram Trading Bot

### Risk-managed trading automation for Bybit Linear USDT, controlled through Telegram

<img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
<img src="https://img.shields.io/badge/Bybit-V5%20API-F7A600?style=flat-square" alt="Bybit V5 API">
<img src="https://img.shields.io/badge/Telegram-Bot-26A5E4?style=flat-square&logo=telegram&logoColor=white" alt="Telegram Bot">
<img src="https://img.shields.io/badge/UI-Russian-6C757D?style=flat-square" alt="Russian UI">
<a href="https://github.com/blackcat-team/Bybit_Trading_bot/actions/workflows/tests.yml">
  <img src="https://github.com/blackcat-team/Bybit_Trading_bot/actions/workflows/tests.yml/badge.svg?branch=main" alt="Tests">
</a>

<br><br>

**English** · [Русский](./README_RU.md)

</div>

---

## About

This is a Telegram-controlled trading bot I built for my own workflow on **Bybit Linear USDT**.

The main idea is simple: define the risk first, calculate the position from that risk, and automate as much of the trade management as possible.

The bot handles position sizing, Stop Loss and Take Profit placement, breakeven logic, signal parsing, monitoring and reporting. It also includes additional safety controls for margin, total open risk, duplicate signals and poorly performing signal sources.

> **Current Telegram interface language: Russian.**
> The codebase and this README can still be useful for anyone interested in the trading logic, risk controls or architecture.

---

## Why I built it

I wanted a workflow where I could send or parse a trade idea and let the bot handle the repetitive parts without manually calculating position size, placing several orders or constantly checking whether a position should already be moved to breakeven.

The project gradually grew from a small Telegram utility into a long-running trading service with its own risk controls, journal, monitoring and deployment setup.

---

## Core features

### 🎯 Fixed risk per trade

Position size is calculated from the entry price, Stop Loss and configured dollar risk.

Instead of choosing a random position size first, the bot works backwards from the amount you are prepared to lose if the stop is hit.

Example:

```text
Risk per trade: $50
Entry:          calculated / supplied
Stop Loss:      supplied
Position size:  calculated automatically
```

The bot also validates available margin and rejects invalid or zero-size orders.

### 📉 Staged breakeven logic

Trade protection changes as the position moves in profit:

```text
Profit >= 1R  -> remaining risk reduced to approximately -0.3R
Profit >= 2R  -> Stop Loss moved to Entry + 0.05R
```

The small positive offset at the second stage is intended to help cover trading costs.

### 💰 Automatic Take Profits

The default strategy splits the position into three targets:

```text
TP1  -> 30% at 1R
TP2  -> 30% at 2R
TP3  -> 40% at 3R
```

### ⏳ Time-based position monitoring

Positions can also generate alerts based on how long they remain open:

```text
5 days -> warning if the trade has not reached 1R
7 days -> critical alert
```

### 📨 Signal parsing

The bot can parse text-based trading signals using formats built around:

```text
COIN ENTRY STOP
```

Signal sources can also be tracked through hashtags.

### 🎛 Telegram control

The bot provides Telegram commands and interactive actions for:

* starting and pausing new trade processing
* changing risk
* checking open positions
* checking active orders
* reviewing current status
* generating reports
* adding journal notes
* market entry
* automatic Take Profits
* emergency position closing

---

## Safety and risk controls

Trading automation becomes much more useful when it also knows when **not** to trade.

The project includes several optional controls.

### Total open risk

```env
MAX_TOTAL_HEAT_USDT=200
HEAT_ACTION=reject
```

`MAX_TOTAL_HEAT_USDT` limits the combined risk currently exposed across open trades.

Available behavior:

```text
reject -> reject the new trade
queue  -> store it in the queue
```

> `queue` currently stores the trade but does not execute it automatically.

### Market confirmation

```env
REQUIRE_MARKET_CONFIRM=1
MARKET_PREVIEW_TTL_SEC=300
```

When enabled, market execution requires confirmation from a Telegram preview instead of immediately sending the order.

### Duplicate signal policy

```env
CONFLICT_POLICY_SAME_DIR=ignore
SOURCE_ALLOW_ADD=0
```

The default configuration prevents another signal from silently adding exposure in the same direction.

### Signal-source quarantine

Sources can be disabled automatically after poor performance:

```env
QUARANTINE_LOSS_STREAK=3
QUARANTINE_DAILY_PNL_USDT=0
QUARANTINE_WEEKLY_PNL_USDT=0
```

This can be based on a loss streak or PnL thresholds.

---

## Bot commands

| Command          | Purpose                                                         |
| ---------------- | --------------------------------------------------------------- |
| `/start`         | Enable new trade processing                                     |
| `/stop`          | Pause new trades                                                |
| `/status`        | Show trading state, PnL, positions, heat, alerts and quarantine |
| `/risk 50`       | Set risk per trade to $50                                       |
| `/pos`           | Show open positions and PnL                                     |
| `/orders`        | Show active orders                                              |
| `/report`        | Generate a trading report                                       |
| `/note BTC Text` | Add a note to the trading journal                               |

The Telegram responses themselves are currently in Russian.

---

## Tech stack

```text
Python 3.10+
├── pybit / Bybit V5 API
├── python-telegram-bot
├── Async application flow
├── JSON / JSONL runtime storage
├── CSV reporting
└── systemd deployment
```

Main repository structure:

```text
Bybit_Trading_bot/
├── app/          # application-level components
├── core/         # trading and risk logic
├── data/         # runtime state and templates
├── deploy/       # deployment files
├── handlers/     # Telegram handlers
├── scripts/      # utility scripts
├── tests/        # automated tests
├── main.py
└── requirements.txt
```

---

## Quick start

### 1. Clone the repository

```bash
git clone https://github.com/blackcat-team/Bybit_Trading_bot.git
cd Bybit_Trading_bot
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create a Telegram bot

Create a bot through **@BotFather** and get:

* Telegram bot token
* your numeric Telegram user ID

The bot restricts access through `ALLOWED_TELEGRAM_ID`.

### 5. Configure environment variables

Copy:

```bash
cp .env.example .env
```

Minimum configuration:

```env
TELEGRAM_TOKEN=YOUR_TELEGRAM_TOKEN
ALLOWED_TELEGRAM_ID=YOUR_TELEGRAM_ID

BYBIT_API_KEY=YOUR_BYBIT_API_KEY
BYBIT_API_SECRET=YOUR_BYBIT_API_SECRET

USER_RISK_USD=50
IS_DEMO=True
```

Additional controls:

```env
MARGIN_BUFFER_USD=1.0
MARGIN_BUFFER_PCT=0.03

MAX_TOTAL_HEAT_USDT=0
HEAT_ACTION=reject
HEAT_QUEUE_TTL_MIN=30

CONFLICT_POLICY_SAME_DIR=ignore
SOURCE_ALLOW_ADD=0

QUARANTINE_LOSS_STREAK=0
QUARANTINE_DAILY_PNL_USDT=0
QUARANTINE_WEEKLY_PNL_USDT=0

REQUIRE_MARKET_CONFIRM=0
MARKET_PREVIEW_TTL_SEC=300
```

---

## Runtime data

The bot keeps its working state inside `data/`.

| File                    | Purpose                         |
| ----------------------- | ------------------------------- |
| `settings.json`         | Trading state and global risk   |
| `risk_data.json`        | Per-symbol risk settings        |
| `journal_comments.json` | Notes created through `/note`   |
| `sources_log.json`      | Signal-source history           |
| `trade_journal.jsonl`   | Append-only trade journal       |
| `disabled_sources.json` | Quarantined sources             |
| `heat_queue.json`       | Trades stored by the heat queue |

Template files are included where applicable.

---

## Running with systemd

A service template is included in:

```text
deploy/bybit-bot.service
```

Typical setup:

```bash
sudo useradd -r -s /sbin/nologin botuser

sudo mkdir -p /opt/bybit-bot
sudo chown -R botuser:botuser /opt/bybit-bot
```

After installing the project and virtual environment:

```bash
sudo cp deploy/bybit-bot.service /etc/systemd/system/bybit-bot.service

sudo systemctl daemon-reload
sudo systemctl enable bybit-bot
sudo systemctl start bybit-bot
```

Status:

```bash
systemctl status bybit-bot
```

Logs:

```bash
journalctl -u bybit-bot -f
```

Restart:

```bash
systemctl restart bybit-bot
```

---

## Suggested rollout

If you are testing the bot for the first time, enable additional automation gradually.

A conservative order is:

| Step | Setting                     | Example                           |
| ---- | --------------------------- | --------------------------------- |
| 1    | Market confirmation         | `REQUIRE_MARKET_CONFIRM=1`        |
| 2    | Total risk ceiling          | `MAX_TOTAL_HEAT_USDT=200`         |
| 3    | Duplicate signal protection | `CONFLICT_POLICY_SAME_DIR=ignore` |
| 4    | Source quarantine           | `QUARANTINE_LOSS_STREAK=3`        |

Start with Bybit demo mode before using live funds.

---

## Before going live

### API

* [ ] Create API credentials with trading permissions only
* [ ] Do not enable withdrawal permissions
* [ ] Use an IP whitelist where possible
* [ ] Confirm `ALLOWED_TELEGRAM_ID` belongs only to you

### Risk

* [ ] Set `USER_RISK_USD` to an amount you are prepared to lose
* [ ] Review the daily loss limit
* [ ] Configure `MAX_TOTAL_HEAT_USDT`
* [ ] Consider enabling `REQUIRE_MARKET_CONFIRM=1`

### Signals

* [ ] Keep duplicate-signal protection enabled
* [ ] Review signal sources before enabling live processing
* [ ] Configure quarantine limits if you use multiple sources

### Runtime

* [ ] Run the service as a non-root user
* [ ] Verify that `data/` is writable
* [ ] Check `/status`
* [ ] Test one small trade end-to-end
* [ ] Verify both Stop Loss and Take Profit orders

---

## Screenshots

Telegram is the main interface for the project.

**Current UI language: Russian.**

Screenshots will be added here to show the real workflow without replacing the actual interface with mockups.

<!--
Suggested screenshots:
1. /status
2. trade preview / confirmation
3. open position with SL / TP
-->

---

## Disclaimer

This project can place real orders on a cryptocurrency exchange.

It is provided for educational and personal-use purposes. Trading involves risk, and bugs, configuration mistakes, API behavior or exchange outages can result in financial loss.

Review the code and configuration yourself before connecting a funded account.

---

## Author

Built and maintained by **BlackCat**.

[GitHub](https://github.com/blackcat-team) · [Telegram](https://t.me/red_tvr) · [X](https://x.com/red_tvr)
