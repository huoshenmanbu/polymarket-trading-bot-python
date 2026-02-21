module.exports = {
  apps: [
    {
      name: "polymarket-bot",
      cwd: "/root/polymarket-trading-bot-python",
      script: "/root/polymarket-trading-bot-python/venv/bin/python",
      interpreter: "none",
      args: "-m src.main",
      autorestart: true,
      watch: false,
      instances: 1,
      max_memory_restart: "1G",
      min_uptime: "10s",
      max_restarts: 10,
      restart_delay: 4000,
      env: {
        PYTHONUNBUFFERED: "1"
      },
      error_file: "/root/polymarket-trading-bot-python/logs/pm2-error.log",
      out_file: "/root/polymarket-trading-bot-python/logs/pm2-out.log",
      merge_logs: true,
      time: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss"
    }
  ]
};