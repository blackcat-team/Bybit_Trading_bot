
# Security Policy

## Supported versions

Until the first tagged public release, security fixes are applied only to the current `main` branch.

Older commits, forks and modified deployments are not actively supported.

## Reporting a vulnerability

Please do not report security vulnerabilities through public GitHub Issues, Discussions or pull requests.

Use GitHub's private vulnerability reporting feature for this repository whenever possible.

When submitting a report, please include:

- a clear description of the issue
- affected component or file
- steps required to reproduce it
- potential impact
- any suggested mitigation, if known

Do not include real API keys, Telegram bot tokens, credentials or other secrets in the report.

## Security-sensitive areas

Issues are especially relevant if they involve:

- authentication or Telegram access control
- exposure of API credentials or tokens
- unintended exchange writes
- order, Stop Loss or Take Profit safety
- risk-control bypasses
- validation failures that can result in unintended trades
- dependency vulnerabilities that can expose secrets or execute code

Trading strategy performance, market losses caused by normal market behavior and general feature requests are not considered security vulnerabilities.

## Responsible testing

Do not test vulnerabilities against accounts, bots, API credentials or exchange resources that you do not own or control.

Do not place live trades, create financial exposure or intentionally disrupt exchange or Telegram services in order to demonstrate a vulnerability.

A minimal reproduction using demo accounts, mocks or isolated test environments is preferred.

## Disclosure

Please allow reasonable time to investigate and fix a reported vulnerability before publishing technical details.

There is currently no guaranteed response-time SLA.

## Secrets

If you accidentally expose an API key, Telegram token or other credential in a public report, revoke or rotate that credential immediately.