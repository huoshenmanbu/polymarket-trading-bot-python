# PM2 运行指南

本指南将帮助你在Linux服务器上使用PM2运行Polymarket交易机器人。

## 前置要求

1. **Python 3.10+** 已安装
2. **PM2** 已安装（如果未安装，见下方安装步骤）
3. 项目依赖已安装（`pip install -r requirements.txt`）
4. 配置文件已设置（`.env`文件）

## 1. 安装PM2

如果服务器上还没有安装PM2，使用以下命令安装：

```bash
# 使用npm安装PM2（需要先安装Node.js）
npm install -g pm2

# 或者使用yarn
yarn global add pm2

# 设置PM2开机自启
pm2 startup
pm2 save
```

## 2. 配置PM2

编辑 `ecosystem.config.js` 文件，修改以下内容：

- **cwd**: 修改为你的项目实际路径（例如：`/home/user/polymarket-trading-bot-python`）
- **interpreter**: 如果你的Python命令是 `python` 而不是 `python3`，请修改为 `python`

## 3. 创建日志目录（可选）

```bash
mkdir -p logs
```

## 4. 启动机器人

### 方式一：使用配置文件启动（推荐）

```bash
pm2 start ecosystem.config.js
```

### 方式二：直接命令行启动

```bash
pm2 start python --name polymarket-bot --interpreter python3 -m src.main
```

或者如果你的Python命令是 `python`：

```bash
pm2 start python --name polymarket-bot --interpreter python -m src.main
```

## 5. 常用PM2管理命令

```bash
# 查看运行状态
pm2 status

# 查看日志（实时）
pm2 logs polymarket-bot

# 查看最近的日志（最后100行）
pm2 logs polymarket-bot --lines 100

# 停止机器人
pm2 stop polymarket-bot

# 重启机器人
pm2 restart polymarket-bot

# 删除进程（停止并移除）
pm2 delete polymarket-bot

# 查看详细信息
pm2 show polymarket-bot

# 监控（CPU、内存使用情况）
pm2 monit

# 保存当前进程列表（用于开机自启）
pm2 save

# 查看所有日志文件
pm2 logs
```

## 6. 设置开机自启

```bash
# 生成启动脚本
pm2 startup

# 按照提示执行生成的命令（通常是sudo开头的命令）

# 保存当前进程列表
pm2 save
```

## 7. 更新代码后重启

```bash
# 拉取最新代码
git pull

# 重启PM2进程
pm2 restart polymarket-bot
```

## 8. 查看错误日志

如果机器人出现问题，可以查看错误日志：

```bash
# 查看错误日志
pm2 logs polymarket-bot --err

# 或者查看日志文件
tail -f logs/pm2-error.log
```

## 9. 环境变量

如果你的 `.env` 文件不在项目根目录，或者需要设置额外的环境变量，可以在 `ecosystem.config.js` 的 `env` 部分添加：

```javascript
env: {
  NODE_ENV: 'production',
  PYTHONPATH: '/path/to/project',
  // 其他环境变量...
}
```

## 注意事项

1. **确保Python路径正确**：使用 `which python3` 或 `which python` 确认Python解释器路径
2. **工作目录**：确保PM2的工作目录（cwd）设置正确
3. **日志轮转**：PM2会自动管理日志，但建议定期清理旧日志
4. **资源监控**：使用 `pm2 monit` 监控CPU和内存使用情况
5. **优雅关闭**：机器人已实现优雅关闭机制，PM2的 `stop` 命令会触发SIGTERM信号

## 故障排查

如果机器人无法启动：

1. **检查Python版本**：`python3 --version` 应该 >= 3.10
2. **检查依赖**：确保已安装所有依赖 `pip install -r requirements.txt`
3. **检查配置文件**：确保 `.env` 文件存在且配置正确
4. **查看错误日志**：`pm2 logs polymarket-bot --err`
5. **手动测试**：先手动运行 `python3 -m src.main` 确认可以正常运行
