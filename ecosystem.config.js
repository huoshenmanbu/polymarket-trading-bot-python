module.exports = {
  apps: [
    {
      name: "polymarket-bot",
      cwd: "/root/polymarket-trading-bot-python",
      script: "src/main.py",
      interpreter: "/root/polymarket-trading-bot-python/venv/bin/python",
      args: "",
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
      time: true
    }
  ]
};