# Foxchase EXZ web dashboard

This is the browser-facing portion of the Foxchase SPY extrinsic-value z-score
study. The repository
intentionally contains no EXZ calculation engine, thresholds, Alpaca
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
git clone https://github.com/adamk/foxchase-exz-dashboard.git
cd foxchase-exz-dashboard
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

## Privacy and data flow

- Your Alpaca credentials are used only by the local connector. They are not
  placed in browser code or sent to Foxchase Trading.
- Historical bars are downloaded through your own Alpaca account and cached
  locally on your computer. Foxchase Trading does not provide or redistribute
  Alpaca market data.
- For a historical calculation, the client sends only the selected,
  normalized bars over HTTPS to the relay. The public relay does not retain
  raw bars; it returns the derived series to the dashboard.
- The proprietary EXZ calculation engine, thresholds, and decision logic are
  not included in this repository and remain server-side.
- The optional homepage presence counter uses only a browser-generated opaque
  session ID with a short expiry. It does not use or store your IP address,
  Alpaca credentials, account information, or market data. Set `presenceUrl`
  to an empty string in `config.js` to disable the heartbeat.

## Historical mode

The example configuration uses the public, historical-only calculation relay:

```js
computeUrl: 'https://exz-api.foxchasetrading.com/api/public/exz/historical'
```

Historical requests are accepted only for completed sessions, use the bars
downloaded with your own Alpaca credentials, and are rate-limited per anonymous
browser session.

## Live mode and private calculation service

An authorized live configuration may instead point to a private endpoint and
include a short-lived token:

```js
window.ZWAP_CONFIG = {
  connectorUrl: 'http://127.0.0.1:8789/api/session',
  computeUrl: 'https://your-private-exz-endpoint/api/v1/live/calculate',
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

For macOS, the one-command launcher starts both local processes and opens the
dashboard automatically. It reuses the existing `~/.foxchase_alpaca_source.env`
file when present; otherwise export your Alpaca keys first:

```bash
./run_exz_local.sh
```

Press **Ctrl-C** in that terminal to stop both processes. The launcher binds
the connector and dashboard to `127.0.0.1`; neither is exposed to the network.

If you prefer to run them separately:

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
