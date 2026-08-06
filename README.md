# Foxchase ZWAP web dashboard

Frontend-only dashboard. This repository intentionally contains no ZWAP
calculation engine, thresholds, Alpaca credentials, or market-data downloader.

For the private local test:

1. Copy `config.example.js` to `config.js` and set the temporary EC2 test token.
2. Run the local connector with the user's own Alpaca credentials:
   `python3 ../zwap_live_dashboard/local_connector.py`
3. Open this directory through a local HTTP server on port 8791.
4. Use an SSH tunnel to the private EC2 compute service (`5070`).

The production version will replace the test token with per-user session
authentication. `config.js` must never be committed.
