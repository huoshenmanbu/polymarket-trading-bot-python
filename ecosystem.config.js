module.exports = {
  apps: [{
    name: 'polymarket-bot',
    script: 'python',
    args: '-m src.main',
    interpreter: 'python3', // 如果系统使用 'python' 命令，请改为 'python'
    cwd: process.cwd(), // 自动使用当前目录，或手动指定：'/path/to/polymarket-trading-bot-python'
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    min_uptime: '10s',
    max_restarts: 10,
    restart_delay: 4000,
    env: {
      NODE_ENV: 'production',
      PYTHONUNBUFFERED: '1' // 确保Python输出不被缓冲
    },
    error_file: './logs/pm2-error.log',
    out_file: './logs/pm2-out.log',
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    merge_logs: true,
    time: true
  }]
};
