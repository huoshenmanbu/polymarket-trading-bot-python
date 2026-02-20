# Security and production checklist

This document summarizes the main security and implementation items to address before using the bot for live trading.

## 1. `.env.backup` and env files

- **Risk:** Setup backs up an existing `.env` to `.env.backup`. If that file were committed, it could leak secrets.
- **Mitigation:**
  - `.env.backup` and `.env.*.backup` are listed in `.gitignore`. Do not remove them.
  - If you ever created `.env.backup` before this was ignored, run:  
    `git rm --cached .env.backup` (if it was committed) and ensure the file is not pushed again.
- **Status:** Addressed in this repo via `.gitignore` and setup message.

## 2. Trade monitor (data source)

- **Risk:** If the trade monitor is not implemented, the executor has no auditable data source; trades could be missed or the pipeline is unclear.
- **Mitigation:**
  - The trade monitor is implemented and uses the **official Polymarket data-api** only:  
    `https://data-api.polymarket.com/activity?user=...&type=TRADE`
  - All ingested trades are written to MongoDB with a well-defined document shape; the executor reads only from these collections.
- **Status:** Implemented in `src/services/trade_monitor.py` (polling-based ingest from data-api).

## 3. CLOB order submission

- **Risk:** Order submission must correctly create API keys, sign orders, and send them to the CLOB; otherwise funds could be at risk or orders could fail silently.
- **Mitigation (implemented):**
  1. **Implemented** in `src/utils/create_clob_client.py` and `src/utils/clob_signing.py`:
     - **L1 auth:** `create_api_key` / `derive_api_key` (EIP-712 signing) for API key creation/derivation.
     - **L2 auth:** HMAC-SHA256 signing for order submission.
     - **Orders:** `create_market_order` builds and signs orders via py-order-utils; `post_order` sends signed orders to the CLOB (FOK/IOC/GTC/GTD).
  2. **Before production you should:**
     - **Audit** the full path: key derivation, order construction, signing, and HTTP submission. Prefer the official [Polymarket CLOB docs](https://docs.polymarket.com/) and reference implementations.
     - **Test** on a small balance or testnet first; confirm orders appear as expected on Polymarket.
     - Ensure system clock is synchronized (NTP); L1 auth rejects timestamps more than 5 minutes off.
- **Status:** CLOB client is implemented. Audit and live testing recommended before production use.

---

## Summary

| Item | Status |
|------|--------|
| **`.env.backup`** | Ignored by git; do not commit any `.env*` backup files. |
| **Trade monitor** | Implemented; data source is the official data-api only. |
| **CLOB client** | Implemented (create/derive API key, create_market_order, post_order). Audit and test before production. |

The codebase is not designed to steal funds; the main risks are env leakage, unclear or missing data ingestion, and incorrect order submission. Addressing this checklist and auditing the CLOB path makes it reasonable to use for live trading after testing.

---

## Before going live (recommended)

- [ ] **Secrets:** Never commit `.env`, `.env.backup`, or any file containing `PRIVATE_KEY` / `PRIVATE_KEYS` / API credentials.
- [ ] **Dependencies:** Run `pip install -r requirements.txt` and consider `pip audit` (or similar) to check for known vulnerabilities.
- [ ] **CLOB audit:** Review `src/utils/clob_signing.py` and `src/utils/create_clob_client.py`; confirm EIP-712 domain, HMAC message format, and order fields match Polymarket’s current API.
- [ ] **Small test:** Run with a small balance; verify a few orders on Polymarket and in MongoDB.
- [ ] **Rate limits:** Be aware of Polymarket/CLOB rate limits; the bot uses retries and backoff but does not enforce a global rate cap.
