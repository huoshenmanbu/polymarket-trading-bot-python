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

## 3. CLOB order submission (placeholders)

- **Risk:** The CLOB client’s order submission (`create_market_order`, `post_order`, and related key/derive logic) is a **placeholder**. No real orders are sent until you implement and wire a production client.
- **Mitigation for production:**
  1. **Implement** in `src/utils/create_clob_client.py`:
     - `create_api_key` / `derive_api_key` (as required by Polymarket).
     - `create_market_order`: build and sign orders in the format required by the CLOB API.
     - `post_order`: send the signed order to the CLOB (e.g. FOK/IOC as needed).
  2. **Audit** the full path: key derivation, order construction, signing, and HTTP/WS submission. Prefer the official Polymarket CLOB docs and reference implementations.
  3. When the placeholder is still in use, the first failed order will trigger a one-time runtime warning that no real orders are being sent.
- **Status:** Placeholder remains; runtime warning added. For live use you must implement and audit the CLOB client as above.

---

## Summary

After handling the above:

1. **`.env.backup`** – Ignored by git; do not commit any `.env*` backup files.
2. **Trade monitor** – Implemented; data source is the official data-api only.
3. **CLOB** – Placeholder; implement and audit order creation and submission before production.

The codebase is not designed to steal funds; the main risks are env leakage, unclear or missing data ingestion, and placeholder order submission. Addressing this checklist makes it safe to use for live trading once the CLOB client is fully implemented and audited.
