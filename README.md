# Quantro

Quantro is a paper-trading terminal and deterministic sandbox execution engine.

Trading is demo-only. Quantro does not support real funds, wallets, deposits,
withdrawals, exchange API keys, private keys, passphrases, or live exchange order
execution. All order responses are produced by Quantro's local paper simulator
with `execution_mode=paper`, `real_funds=false`, and `venue_routing=disabled`.

## Local Start

Install dependencies once:

```bash
python -m venv venv
venv/bin/pip install -e '.[dev]'
cd frontend
npm install
```

Start the backend from the source tree in safe no-outbound demo mode:

```bash
PYTHONPATH=src QUANTRO_MARKET_DATA_PROVIDER=demo venv/bin/python -c "import uvicorn; from quantro_api.app import create_app; uvicorn.run(create_app(), host='127.0.0.1', port=8000)"
```

Start the frontend:

```bash
cd frontend
VITE_API_BASE_URL=http://127.0.0.1:8000 VITE_ENABLE_SANDBOX_AUTH=true npm run dev -- --host 127.0.0.1 --port 5173
```

Open `http://127.0.0.1:5173/` and use **Start Sandbox Account**.

## Market Data Modes

Safe local demo mode:

```bash
QUANTRO_MARKET_DATA_PROVIDER=demo
```

This uses deterministic local BTC-USDT-SWAP and ETH-USDT-SWAP market data. It
makes no outbound exchange requests and supports the full paper-trading flow.

No-market-data mode:

```bash
QUANTRO_MARKET_DATA_PROVIDER=none
```

This disables external and demo market data. Market-data endpoints report
unavailable/not found, and paper swap order placement is blocked.

Optional OKX public read-only mode:

```bash
QUANTRO_MARKET_DATA_PROVIDER=okx
QUANTRO_MARKET_DATA_SYMBOLS=BTC-USDT-SWAP,ETH-USDT-SWAP
OKX_REST_BASE_URL=https://www.okx.com
OKX_WS_PUBLIC_URL=wss://ws.okx.com:8443/ws/v5/public
```

OKX is used only for public market data. Quantro does not call OKX private,
account, wallet, or trade endpoints, and no OKX credentials are accepted.

## Tests

Backend:

```bash
venv/bin/pytest
venv/bin/ruff check .
venv/bin/python -m mypy src/quantro_api/service.py src/quantro_api/app.py src/quantro_api/market_data.py
```

Frontend:

```bash
cd frontend
npm run lint
npm test
npm run build
```

Project-wide mypy currently has pre-existing typing debt outside the MVP path.
Do not treat that as a first-user blocker unless the failing area is being
changed.

## Known Limitations

- Quantro is paper/demo only and has no real-money funding or withdrawal flow.
- Swap leverage, margin controls, TP/SL, and liquidation simulation are not
  implemented.
- Local demo market data is deterministic and intended for onboarding/testing,
  not for market research.
- OKX mode depends on public exchange availability and rate limits.
- Persistent deployments with existing state reseed missing paper markets on
  startup, but old user accounts may need refreshed virtual balances.
