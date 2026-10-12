# CryptoLoveBot — Server
https://www.youtube.com/watch?v=cGCE0FWduUU

https://www.youtube.com/watch?v=sC6boK-1lkc&t=5s

**The public backend and integration layer for CryptoLoveBot, an automated trading product built around Hyperliquid.**

[Website](https://cryptolovebot.com) · [Frontend repository](https://github.com/uni100h-ops/cryptolovebot-web) · [Hyperliquid agent management](https://app.hyperliquid.xyz/API)

This repository provides authentication, wallet authorization, account reporting, fee accounting, email delivery, and supervision of per-user trading processes.

**The trading strategy engine is private and runs on Hetzner. It is not included in this repository or the frontend repository.** The worker imports that engine from a separately installed directory. Both public repositories make the surrounding application available for review, but they do not provide a standalone reproduction of the trading strategy.

## Responsibilities

| Module | Responsibility |
| --- | --- |
| `app.py` | FastAPI routes, authentication, session cookies, wallet signatures, account controls, fee payment |
| `core.py` | SQLite persistence, encrypted agent keys, user locks, mail queue, fee arithmetic |
| `worker.py` | Subscriber process supervision, integration with the private engine, fill reconciliation, email delivery |
| `billing.py` | Fee deadlines, reminders, reconciliation holds, overdue-account cleanup |
| `public_audit.py` | Read-only public master-account performance reporting |
| `.env.example` | Environment configuration template |
| `requirements.txt` | Python dependencies |
| `install.sh` | Linux installation and systemd service definitions |

The application uses FastAPI, Uvicorn, SQLite, Argon2, Fernet encryption, Ethereum message signatures, the Hyperliquid Python SDK, and SMTP. Its process locks use Linux `fcntl`.

## How an account is connected

1. The user creates an account and verifies their email. Google sign-in is optional and also follows the application's confirmation flow.
2. MetaMask signs a wallet-ownership challenge.
3. The server creates a dedicated API agent key and stores it encrypted. The user signs the Hyperliquid agent approval in MetaMask.
4. The worker starts a subscriber process that loads the private engine and supplies that user's account and agent credentials.
5. Execution records are reconciled against fills and funding before a trade is finalized in the private dashboard.
6. Accrued service fees are paid through a separate wallet-signed transfer.

Funds remain in the user's Hyperliquid account. The application does not request a seed phrase or the master wallet's private key. The trading agent can place orders, which can lose money. Agent authorization is revocable through Hyperliquid.

## Private engine integration

The worker loads the engine from `STRATEGY_DIR` and reads its configuration from `STRATEGY_CONFIG`.

The expected modules include:

- `agente_real.py`: `Runner`, `fingerprint`, and runner integration points.
- `infraestructura.py`: `read_config`, `Store`, `Audit`, `API`, `InstanceLock`, and `Rejected`.
- `cuenta.py`: the strategy's `Account` implementation.

The engine and all its own dependencies must be installed separately. The integration wraps account operations and reporting while delegating the underlying strategy to those private modules.

Each subscriber receives separate state under `DATA_DIR/subscribers/<user_id>`. The wrapper removes master-account credentials from the temporary configuration and supplies the subscriber's decrypted agent key in memory. The strategy's `API` setting must match the selected `HL_NETWORK`.

The wrapper checks for at least **100 USDC of account equity** before new entries. Actual order eligibility also depends on the engine and exchange constraints. Configuration changes are rejected when a subscriber has open positions and the stored strategy fingerprint differs.

## Inspect the API locally

Use Linux with a Python version compatible with `requirements.txt` and virtual-environment support. This example starts only the API; it does not start the trading worker.

```bash
git clone https://github.com/uni100h-ops/cryptolovebot-server.git
cd cryptolovebot-server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Generate the two required secrets:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Put the first value in `APP_SECRET` and the second in `ENCRYPTION_KEY`. For local API inspection, set `DATA_DIR=./data`, retain `HL_NETWORK=testnet` and `TRADING_ENABLED=false`, and configure the frontend and API URLs for your environment.

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8091 --workers 1
```

In another terminal:

```bash
curl http://127.0.0.1:8091/health
curl http://127.0.0.1:8091/public/config
```

Full browser authentication requires HTTPS: session cookies use `Secure`, `HttpOnly`, `SameSite=None`, and the `__Host-` prefix. POST requests must supply the configured frontend `Origin` and `X-CryptoLove: 1`. Interactive Swagger and ReDoc pages are disabled.

Do not treat `TRADING_ENABLED=false` as a full dry-run mode. It blocks new entries and related opening actions; an already authorized subscriber process can still manage existing positions. Running `worker.py` also processes email, fee deadlines, and public reporting.

## Environment configuration

| Variable | Purpose |
| --- | --- |
| `FRONTEND_URL` | Exact frontend origin used for CORS, POST validation, and emailed links |
| `API_PUBLIC_URL` | Public API base URL |
| `APP_SECRET` | Application signing secret, at least 40 characters |
| `ENCRYPTION_KEY` | Fernet key used to encrypt and decrypt API agent credentials |
| `DATA_DIR` | Persistent database, report, lock, and subscriber-state directory |
| `HL_NETWORK` | `testnet` or `mainnet`; default is `testnet` |
| `TRADING_ENABLED` | Must be exactly `true` to allow new entries through the wrapper |
| `MAX_SUBSCRIBERS` | Subscriber capacity setting; default is `10` |
| `STRATEGY_DIR` | Directory containing the private trading engine |
| `STRATEGY_CONFIG` | Private engine configuration file |
| `FEE_BASIS` | `profit` by default; alternatively `margin` |
| `TREASURY_ADDRESS` | Wallet receiving user-authorized service-fee payments |
| `PUBLIC_MASTER_WALLET` | Public mainnet account used for performance reporting |
| `GOOGLE_CLIENT_ID` | Optional Google sign-in client ID |
| `SMTP_HOST`, `SMTP_PORT` | Outgoing email host and port |
| `SMTP_FROM`, `SMTP_USER`, `SMTP_PASSWORD` | Email sender and SMTP credentials |

For the project's domains, the intended URL pair is `https://cryptolovebot.com` and `https://api.cryptolovebot.com`. Production values must match the actual deployment. Keep `.env`, encryption keys, databases, and subscriber state outside Git history. Backups of encrypted state require the original encryption key for recovery.

## Linux and Hetzner deployment

The included installer currently uses the legacy directory **`/opt/cryptolove`**, user **`cryptolove`**, and service names **`cryptolove-api.service`** and **`cryptolove-worker.service`**. It rejects other installation directories. The product's new name does not automatically change these paths.

When preparing a new installation, review `install.sh` before running it. It installs dependencies and writes systemd units, but does not start them. It also references a `PASO_A_PASO.md` file that is not present in this public snapshot.

The API unit listens on `127.0.0.1:8091` and is intended to sit behind Caddy with HTTPS. The worker unit supervises the subscriber processes. Both require persistent, writable `DATA_DIR` storage and appropriate access to the private engine. The supplied systemd sandbox permits writes to `/opt/cryptolove/data`; changing paths requires corresponding unit changes.

For an existing deployment using `/opt/cryptolovebot` or renamed services, retain its actual configuration and review the differences instead of applying the legacy installer over it.

## API surface

| Area | Routes |
| --- | --- |
| Health and public reporting | `GET /health`, `GET /public/config`, `GET /public/performance`, `GET /public/audit` |
| Registration and authentication | `POST /auth/signup`, `/auth/verify`, `/auth/resend`, `/auth/login`, `/auth/google`, `/auth/logout` |
| Password recovery | `POST /auth/forgot`, `/auth/reset` |
| Current account | `GET /me`, `GET /trades` |
| Wallet ownership | `POST /wallet/challenge`, `/wallet/link` |
| Agent authorization | `POST /wallet/prepare`, `/wallet/authorize` |
| Pause or resume entries | `POST /subscription` |
| Service-fee payment | `POST /fees/prepare`, `/fees/pay` |
| Account removal | `POST /account/delete`, `/auth/unsubscribe` |

The public `/public/audit` endpoint provides sampled portfolio data. It is not an execution-log endpoint and is not interchangeable with `/public/account-log`.

## Trade accounting and fees

For a reconciled trade, the worker calculates net profit from closing PnL, subtracts opening and closing execution fees, and adds attributed funding. Entry margin is calculated from execution price, size, and leverage.

With the default profit-based model:

```text
net_return_pct = net_profit / entry_margin × 100
service_fee = net_profit × 0.01, only when net_return_pct > 1
```

At exactly 1% or below, the service fee is zero. Fees are rounded down to six decimal places. Example: a 10 USDC net profit on 100 USDC entry margin produces a 10% return and a 0.10 USDC service fee. If `FEE_BASIS=margin`, the 1% fee instead applies to entry margin after the same profitability threshold. This is per-trade billing, not a portfolio high-water mark.

The worker leaves incomplete or inconsistent trades in reconciliation rather than finalizing their fee. Dashboard balances are account-equity observations after reconciliation and may include other open positions; unpaid service fees have not been deducted.

Payments require the account owner's separate signature. If an exchange response is uncertain, the invoice remains unresolved and repeat payment is blocked pending operator reconciliation.

Fees are due within 48 hours of the oldest pending fee, with reminders every 8 hours. The current implementation deletes an overdue unpaid profile and stops its automation, except while a payment is submitting or uncertain. New fees, restarts, and signing in do not extend an existing deadline.

Deleting a profile does not close Hyperliquid positions or revoke the exchange authorization. Exchange-native orders remain, but logical strategy stops stop being managed. The user must manage positions and revoke the agent in Hyperliquid.

## Public performance methodology

`public_audit.py` reads the configured master wallet's **mainnet** perpetual-account portfolio even when subscriber testing uses testnet. Its configured start is **10 October 2026, 00:00 Europe/Madrid**.

It stores equity and PnL samples, aligns overlapping exchange history windows, infers external cash flows, and estimates return using Modified Dietz. The figure includes unrealized PnL and is not exact time-weighted return. Missing or inconsistent baseline data prevents publication of an invented percentage; previously available data can be marked stale after retrieval failures.

The supervisor attempts an update every five minutes. A Hypurrscan link supports independent inspection of the account; this is not a certified financial or security audit.

## Public repository integration status

Source review date: **12 October 2026**. The current public server and frontend have these interface differences:

- `Signup` requires `username`, while the current frontend signup form submits email and password only.
- `safe_user()` omits `verified`, but the frontend requires that field to reveal the private area and allow wallet connection.
- The frontend account-log page requests `GET /public/account-log`; that route is absent from this public `app.py`.
- The frontend reads `supportEmail` from `/public/config`, but the public backend does not provide it.

These are findings about the reviewed source, not a verification of the running Hetzner deployment. Align the public snapshots before expecting a complete frontend-to-backend demonstration. The private strategy engine must also be available to review strategy behavior or reproduce trades.

## Project status and license

CryptoLoveBot is an early-stage product under active development. Work continues on integration and validation between historical simulation and live execution. Identical trades and returns across simulations or accounts are not guaranteed. Automated trading can lose money.

No license file was present in the reviewed repository. Public source visibility does not by itself grant open-source reuse rights. The private strategy engine and production credentials are not part of this repository.
