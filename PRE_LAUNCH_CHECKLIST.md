# 上线前检查清单

正式用真金白银跟单前，请逐项完成本清单。对应安全说明见 [SECURITY.md](SECURITY.md)。

---

## 一、环境与密钥

- [ ] **未提交敏感文件**  
  确认未把 `.env`、`.env.backup`、`.env.*.backup` 或任何含 `PRIVATE_KEY` / `PRIVATE_KEYS` / API 密钥的文件提交到 Git。  
  - 若曾误提交过：`git rm --cached .env.backup` 并确保不再 push。

- [ ] **.gitignore 包含备份**  
  确认 `.gitignore` 中有：`.env`、`.env.backup`、`.env.*.backup`。

- [ ] **环境变量完整**  
  对照 `.env.example` 填好所有必填项（如 `USER_ADDRESSES`、`PROXY_WALLETS`、`PRIVATE_KEYS`、`CLOB_HTTP_URL`、`RPC_URL`、`MONGO_URI` 等）。

- [ ] **系统时间同步**  
  运行环境已启用 NTP（或等效）；L1 认证对时间偏差超过约 5 分钟会失败。

---

## 二、依赖与运行环境

- [ ] **依赖安装**  
  已执行 `pip install -r requirements.txt`，无报错。

- [ ] **依赖安全（可选）**  
  已运行 `pip audit`（或类似工具）检查已知漏洞，并按需升级。

- [ ] **Python 版本**  
  使用项目要求的 Python 版本（建议 3.10+）。

---

## 三、CLOB 与下单逻辑

- [ ] **CLOB 代码审计**  
  已阅读并理解：  
  - `src/utils/clob_signing.py`（EIP-712 签名、HMAC L2 签名、时间戳与 nonce 校验）  
  - `src/utils/create_clob_client.py`（API 密钥创建/派生、订单构建与提交）  
  并与 [Polymarket CLOB 文档](https://docs.polymarket.com/) 对照，确认 domain、HMAC 消息格式、订单字段与当前 API 一致。

- [ ] **钱包类型与配置**  
  确认 `PROXY_WALLETS` / `PRIVATE_KEYS` 与真实使用方式一致（EOA 直连 或 Polymarket 代理/ Gnosis Safe），且每个钱包能成功 create/derive API key。

- [ ] **小额或测试网验证**  
  使用小额资金（或测试网若支持）实际跑几笔：  
  - 跟单能触发、订单能成功提交；  
  - 在 Polymarket 前端能看到对应订单/持仓；  
  - MongoDB 中跟单记录（如 `bot`、`botExcutedTime`）符合预期。

---

## 四、数据与限流

- [ ] **数据源确认**  
  确认跟单数据仅来自官方 data-api（`src/services/trade_monitor.py` 中的 `https://data-api.polymarket.com/activity`），未接入不可信数据源。

- [ ] **限流与重试**  
  已知晓 Polymarket/CLOB 可能有频率限制；当前机器人有重试与退避，但无全局速率上限，高并发时需自行控制或观察。

---

## 五、上线当日

- [ ] **先用小仓位**  
  首日建议用较小 `COPY_SIZE` / 较小余额，观察数笔成交与余额变化无误后再放大。

- [ ] **监控与日志**  
  确认有办法查看运行日志和错误信息（如 401、余额不足、限流等），便于第一时间发现问题。

- [ ] **可随时停**  
  知道如何停止机器人（如 Ctrl+C 或停止进程），避免在异常情况下继续下单。

---

## 签字（可选）

完成上述项后，可在此记录日期与备注，便于日后回溯：

- 完成日期： _______________
- 备注（如「仅完成 1–3 项，第 4 项已知晓」）： _______________

---

*本清单与 [SECURITY.md](SECURITY.md) 配套使用；如有更新以 SECURITY.md 及实际代码为准。*
