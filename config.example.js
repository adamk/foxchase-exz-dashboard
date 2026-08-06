// Copy to config.js for local testing. Do not commit config.js or tokens.
window.ZWAP_CONFIG = {
  connectorUrl: 'http://127.0.0.1:8789/api/session',
  // Historical calculations are free and rate-limited. Live/current-day
  // access uses a separate authenticated endpoint supplied after purchase.
  computeUrl: 'https://bot.foxchasetrading.com/api/public/zwap/historical',
  computeToken: '',
  presenceUrl: 'http://127.0.0.1:5070/api/v1/presence'
};
