# Foxchase ZWAP web dashboard

This is the browser-facing portion of the Foxchase ZWAP study. The repository
intentionally contains no ZWAP calculation engine, thresholds, Alpaca
credentials, or market-data downloader. The browser sends user-owned,
historical SPY bars to a rate-limited historical relay and renders the returned
series. The relay forwards the request to the private calculation engine.

## Requirements

- Python 3.10 or newer (the connector uses only the standard library).
- Your own Alpaca market-data API credentials.
- Alpaca Algo Trader Plus for archived historical option sessions. Alpaca's
  free Basic plan is limited to recent data and is not sufficient for this
  historical workflow.
- For historical mode, no Foxchase token is required; the public historical
  relay is rate-limited.
- Live/current-day mode requires a separate paid entitlement and an authorized
  connection to the private live calculation service.

## Install

```bash
git clone https://github.com/adamk/foxchase-zwap-dashboard.git
cd foxchase-zwap-dashboard
python3 --version
cp config.example.js config.js
```

Do not commit `config.js`. It may contain a private live calculation-service
token if you are authorized for live access.

## Configure Alpaca

Export your own keys in the terminal where the local connector will run:

```bash
export APCA_API_KEY_ID="your_alpaca_key"
export APCA_API_SECRET_KEY="your_alpaca_secret"
```

The connector keeps those credentials local and sends only the requested,
normalized historical bars to the calculation service. They are never placed
in the browser code or sent to Foxchase's public website.

## Historical mode

The example configuration uses the public, historical-only calculation relay:

```js
computeUrl: 'https://bot.foxchasetrading.com/api/public/zwap/historical'
```

Historical requests are accepted only for completed sessions, use the bars
downloaded with your own Alpaca credentials, and are rate-limited per anonymous
browser session. No raw bars are stored by the relay.

## Live mode and private calculation service

An authorized live configuration may instead point to a private endpoint and
include a short-lived token:

```js
window.ZWAP_CONFIG = {
  connectorUrl: 'http://127.0.0.1:8789/api/session',
  computeUrl: 'https://your-private-zwap-endpoint/api/v1/live/calculate',
  computeToken: 'YOUR_SHORT_LIVED_LIVE_TOKEN',
  presenceUrl: 'http://127.0.0.1:5070/api/v1/presence'
};
```

If the service is reached through SSH, create a local tunnel in a second
terminal. Replace the host and user with the values supplied by the service
operator:

```bash
ssh -N -L 5070:127.0.0.1:5070 YOUR_USER@YOUR_PRIVATE_HOST
```

The live service token is not included in this repository. Each authorized
user must receive their own short-lived token through a separate secure
activation flow.

## Run locally

Start the user-side Alpaca connector:

```bash
python3 local_connector.py
```

In a second terminal, serve the dashboard:

```bash
python3 -m http.server 8791
```

Open [http://127.0.0.1:8791/](http://127.0.0.1:8791/) in your browser. Choose a
completed trading date, select ATM or a strike offset, and press **Load
session**. The first request downloads and caches the selected session locally;
later views of the same date and strike are faster.

The dashboard deliberately blocks the current day and future dates in the
historical view. Live-session access is a separate permissioned service.

## Files and security

- `index.html` and `app.js`: dashboard UI and chart rendering.
- `local_connector.py` and `zwap_client.py`: local, user-owned Alpaca adapter.
- `config.example.js`: safe configuration template.
- `config.js`: local configuration that may contain a live token; never commit
  or publish it.

This project is for research and visualization, not trade execution. Keep API
credentials, service tokens, and cached market data private, and follow the
terms of your data provider.
