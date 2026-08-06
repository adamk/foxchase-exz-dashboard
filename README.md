# Foxchase ZWAP web dashboard

This is the browser-facing portion of the Foxchase ZWAP study. The repository
intentionally contains no ZWAP calculation engine, thresholds, Alpaca
credentials, or market-data downloader. The browser sends user-owned,
historical SPY bars to a private calculation service and renders the returned
series.

## Requirements

- Python 3.10 or newer (the connector uses only the standard library).
- Your own Alpaca market-data API credentials.
- Alpaca Algo Trader Plus for archived historical option sessions. Alpaca's
  free Basic plan is limited to recent data and is not sufficient for this
  historical workflow.
- An authorized connection to a compatible private ZWAP calculation service.
  The public repository does not include that service or its access token.

## Install

```bash
git clone https://github.com/adamk/foxchase-zwap-dashboard.git
cd foxchase-zwap-dashboard
python3 --version
cp config.example.js config.js
```

Do not commit `config.js`. It contains the private calculation-service token.

## Configure Alpaca

Export your own keys in the terminal where the local connector will run:

```bash
export APCA_API_KEY_ID="your_alpaca_key"
export APCA_API_SECRET_KEY="your_alpaca_secret"
```

The connector keeps those credentials local and sends only the requested,
normalized historical bars to the calculation service. They are never placed
in the browser code or sent to Foxchase's public website.

## Connect to the calculation service

The default `config.js` expects the service on local port `5070`:

```js
window.ZWAP_CONFIG = {
  connectorUrl: 'http://127.0.0.1:8789/api/session',
  computeUrl: 'http://127.0.0.1:5070/api/v1/historical/calculate',
  computeToken: 'YOUR_PRIVATE_SERVICE_TOKEN',
  presenceUrl: 'http://127.0.0.1:5070/api/v1/presence'
};
```

If the service is reached through SSH, create a local tunnel in a second
terminal. Replace the host and user with the values supplied by the service
operator:

```bash
ssh -N -L 5070:127.0.0.1:5070 YOUR_USER@YOUR_PRIVATE_HOST
```

The service token is not included in this repository. Each authorized user
must receive their own token through a separate secure channel.

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
historical view. Live-session access, if offered, is a separate permissioned
service.

## Files and security

- `index.html` and `app.js`: dashboard UI and chart rendering.
- `local_connector.py` and `zwap_client.py`: local, user-owned Alpaca adapter.
- `config.example.js`: safe configuration template.
- `config.js`: local secret-bearing configuration; never commit or publish it.

This project is for research and visualization, not trade execution. Keep API
credentials, service tokens, and cached market data private, and follow the
terms of your data provider.
