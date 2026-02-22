"""
Trade executor service - executes trades based on monitored activity
"""
import asyncio
import time
import urllib.parse
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from ..config.env import ENV
from ..models.user_history import get_user_activity_collection
from ..interfaces.user import UserActivityInterface, UserPositionInterface
from ..utils.fetch_data import fetch_data_async
from ..utils.get_my_balance import get_my_balance_async
from ..utils.post_order import post_order
from ..utils.logger import (
    success, info, warning, error, header, waiting, clear_line, separator, trade as log_trade, balance as log_balance,
)

USER_ADDRESSES = ENV.USER_ADDRESSES
RETRY_LIMIT = ENV.RETRY_LIMIT
TRADE_AGGREGATION_ENABLED = ENV.TRADE_AGGREGATION_ENABLED
TRADE_AGGREGATION_WINDOW_SECONDS = ENV.TRADE_AGGREGATION_WINDOW_SECONDS
TRADE_AGGREGATION_MIN_TOTAL_USD = ENV.TRADE_AGGREGATION_MIN_USD

GAMMA_EVENTS_BASE = 'https://gamma-api.polymarket.com/events/slug'

is_running = True

# Unix timestamp (seconds) when trade_executor started. Trades with an API timestamp
# before this value are ignored so that restarting the bot never re-executes old trades.
_startup_time_sec: float = 0.0

# Type definitions (using Dict for flexibility)
TradeWithUser = Dict[str, Any]
AggregatedTrade = Dict[str, Any]
# (proxy_wallet, clob_client) per follow wallet
FollowEntry = Tuple[str, Any]

# Single global round-robin index: each executed trade (single or aggregated) consumes one slot.
_next_follow_index: int = 0

# Buffer for aggregating trades
trade_aggregation_buffer: Dict[str, AggregatedTrade] = {}


def get_next_follow(follow_list: List[FollowEntry]) -> FollowEntry:
    """Return (proxy_wallet, clob_client) for the next trade. Does not retry with another wallet on failure."""
    global _next_follow_index
    if not follow_list:
        raise ValueError('follow_list must not be empty')
    idx = _next_follow_index % len(follow_list)
    _next_follow_index += 1
    return follow_list[idx]


def _normalize_ts_to_sec(ts: Any) -> float:
    """Convert API timestamp to seconds. Supports both seconds and milliseconds."""
    if not ts:
        return 0.0
    ts_f = float(ts)
    return ts_f / 1000.0 if ts_f > 1e12 else ts_f


async def read_temp_trades() -> List[TradeWithUser]:
    """Read unprocessed trades from database, only returning trades that occurred
    after the bot started (based on the API-provided timestamp)."""
    all_trades: List[TradeWithUser] = []
    
    for address in USER_ADDRESSES:
        collection = get_user_activity_collection(address)
        # Only get trades that haven't been processed yet (bot: false AND botExcutedTime: 0)
        # This prevents processing the same trade multiple times
        trades = list(collection.find({
            'type': 'TRADE',
            'bot': False,
            'botExcutedTime': 0
        }))
        
        for trade in trades:
            # Skip trades that happened before this bot run started
            trade_ts = _normalize_ts_to_sec(trade.get('timestamp'))
            if trade_ts > 0 and trade_ts < _startup_time_sec:
                continue
            trade['userAddress'] = address
            all_trades.append(trade)

    # Sort by timestamp so round-robin follows time order; put missing timestamp at end
    all_trades.sort(key=lambda t: (t.get('timestamp') is None, t.get('timestamp') or 0))
    return all_trades


async def is_market_ended(slug: str) -> Optional[bool]:
    """
    Fetch event by slug from Gamma API; return True if endDate is in the past,
    False if still open, None if unknown (no slug, API error, or no endDate).
    Used to skip trades that are likely settlement/redemption (on-chain after market close).
    """
    if not slug or not str(slug).strip():
        return None
    try:
        url = f'{GAMMA_EVENTS_BASE}/{urllib.parse.quote(str(slug).strip())}'
        data = await fetch_data_async(url)
        if not data or not isinstance(data, dict):
            return None
        end_date_str = data.get('endDate')
        if not end_date_str:
            return None
        # Parse ISO format e.g. 2026-02-22T08:45:00.000Z
        end_date_str = end_date_str.replace('Z', '+00:00')
        end_dt = datetime.fromisoformat(end_date_str)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        end_ts = end_dt.timestamp()
        return end_ts < time.time()
    except Exception:
        return None


def get_aggregation_key(trade: TradeWithUser) -> str:
    """Generate a unique key for trade aggregation based on user, market, side"""
    return f"{trade['userAddress']}:{trade.get('conditionId', '')}:{trade.get('asset', '')}:{trade.get('side', 'BUY')}"


def add_to_aggregation_buffer(trade: TradeWithUser) -> None:
    """Add trade to aggregation buffer or update existing aggregation"""
    key = get_aggregation_key(trade)
    existing = trade_aggregation_buffer.get(key)
    now = int(time.time() * 1000)  # milliseconds
    
    if existing:
        # Update existing aggregation
        existing['trades'].append(trade)
        existing['totalUsdcSize'] += trade.get('usdcSize', 0)
        # Recalculate weighted average price
        total_value = sum(t.get('usdcSize', 0) * t.get('price', 0) for t in existing['trades'])
        existing['averagePrice'] = total_value / existing['totalUsdcSize'] if existing['totalUsdcSize'] > 0 else 0
        existing['lastTradeTime'] = now
    else:
        # Create new aggregation
        trade_aggregation_buffer[key] = {
            'userAddress': trade['userAddress'],
            'conditionId': trade.get('conditionId', ''),
            'asset': trade.get('asset', ''),
            'side': trade.get('side', 'BUY'),
            'slug': trade.get('slug'),
            'eventSlug': trade.get('eventSlug'),
            'trades': [trade],
            'totalUsdcSize': trade.get('usdcSize', 0),
            'averagePrice': trade.get('price', 0),
            'firstTradeTime': now,
            'lastTradeTime': now,
        }


def get_ready_aggregated_trades() -> List[AggregatedTrade]:
    """Check buffer and return ready aggregated trades
    Trades are ready if:
    1. Total size >= minimum AND
    2. Time window has passed since first trade
    """
    ready: List[AggregatedTrade] = []
    now = int(time.time() * 1000)  # milliseconds
    window_ms = TRADE_AGGREGATION_WINDOW_SECONDS * 1000
    
    keys_to_remove = []
    
    for key, agg in trade_aggregation_buffer.items():
        time_elapsed = now - agg['firstTradeTime']
        
        # Check if aggregation is ready
        if time_elapsed >= window_ms:
            if agg['totalUsdcSize'] >= TRADE_AGGREGATION_MIN_TOTAL_USD:
                # Aggregation meets minimum and window passed - ready to execute
                ready.append(agg)
            else:
                # Window passed but total too small - mark individual trades as skipped
                info(
                    f"Trade aggregation for {agg['userAddress']} on {agg.get('slug') or agg.get('asset', 'unknown')}: "
                    f"${agg['totalUsdcSize']:.2f} total from {len(agg['trades'])} trades below minimum "
                    f"(${TRADE_AGGREGATION_MIN_TOTAL_USD}) - skipping"
                )
                
                # Mark all trades in this aggregation as processed (bot: true)
                for trade in agg['trades']:
                    collection = get_user_activity_collection(trade['userAddress'])
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
            
            # Remove from buffer either way
            keys_to_remove.append(key)
    
    for key in keys_to_remove:
        del trade_aggregation_buffer[key]
    
    return ready


async def do_trading(follow_list: List[FollowEntry], trades: List[TradeWithUser]) -> None:
    """Execute trades. Each trade uses the next follow wallet (round-robin). Failure does not retry with another wallet."""
    for trade in trades:
        # Mark trade as being processed immediately to prevent duplicate processing
        collection = get_user_activity_collection(trade['userAddress'])
        collection.update_one(
            {'_id': trade['_id']},
            {'$set': {'botExcutedTime': 1}}
        )
        
        log_trade(
            trade['userAddress'],
            trade.get('side', 'UNKNOWN'),
            {
                'asset': trade.get('asset'),
                'side': trade.get('side'),
                'amount': trade.get('usdcSize'),
                'price': trade.get('price'),
                'slug': trade.get('slug'),
                'eventSlug': trade.get('eventSlug'),
                'transactionHash': trade.get('transactionHash'),
                'timestamp': trade.get('timestamp'),
            }
        )
        # Skip BUY if market has already ended (trade may be settlement/redemption, not copyable)
        # Do this BEFORE get_next_follow so we don't waste a round-robin slot on skipped trades.
        if trade.get('side') == 'BUY':
            slug = trade.get('eventSlug') or trade.get('slug') or ''
            ended = await is_market_ended(slug)
            if ended is True:
                info('Market already ended (trade likely settlement/redemption), skipping')
                collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                separator()
                continue

        proxy_wallet, clob_client = get_next_follow(follow_list)
        try:
            my_positions_data = await fetch_data_async(f'https://data-api.polymarket.com/positions?user={proxy_wallet}')
            user_positions_data = await fetch_data_async(f'https://data-api.polymarket.com/positions?user={trade["userAddress"]}')
            
            my_positions_list = my_positions_data if isinstance(my_positions_data, list) else []
            user_positions_list = user_positions_data if isinstance(user_positions_data, list) else []
            
            my_position = next(
                (p for p in my_positions_list if p.get('conditionId') == trade.get('conditionId')),
                None
            )
            user_position = next(
                (p for p in user_positions_list if p.get('conditionId') == trade.get('conditionId')),
                None
            )
            
            # Get USDC balance for this follow wallet
            my_balance = await get_my_balance_async(proxy_wallet)
            
            # Calculate trader's total portfolio value from positions
            user_balance = sum(pos.get('currentValue', 0) or 0 for pos in user_positions_list)
            
            log_balance(my_balance, user_balance, trade['userAddress'])
            
            # Execute the trade: use 'merge' for SELL so we actually sell our position (sell branch is not implemented)
            await post_order(
                clob_client,
                'buy' if trade.get('side') == 'BUY' else 'merge',
                my_position,
                user_position,
                trade,
                my_balance,
                user_balance,
                trade['userAddress']
            )
        except Exception as e:
            error(
                f'Trade execution failed for {trade.get("slug") or trade.get("asset", "?")} '
                f'(follow wallet {proxy_wallet[:10]}...): {e}'
            )
            warning('Trade marked as processed; will not retry with another wallet.')
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
        separator()


async def do_aggregated_trading(follow_list: List[FollowEntry], aggregated_trades: List[AggregatedTrade]) -> None:
    """Execute aggregated trades. Each aggregated trade uses the next follow wallet (round-robin)."""
    for agg in aggregated_trades:
        header(f"AGGREGATED TRADE ({len(agg['trades'])} trades combined)")
        info(f"Market: {agg.get('slug') or agg.get('asset', 'unknown')}")
        info(f"Side: {agg.get('side', 'BUY')}")
        info(f"Total volume: ${agg['totalUsdcSize']:.2f}")
        info(f"Average price: ${agg['averagePrice']:.4f}")
        
        # Mark all individual trades as being processed
        for trade in agg['trades']:
            collection = get_user_activity_collection(trade['userAddress'])
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'botExcutedTime': 1}}
            )
        # Skip BUY if market has already ended.
        # Do this BEFORE get_next_follow so we don't waste a round-robin slot on skipped trades.
        if agg.get('side') == 'BUY':
            first_trade = agg['trades'][0] if agg.get('trades') else {}
            slug = agg.get('eventSlug') or agg.get('slug') or first_trade.get('eventSlug') or first_trade.get('slug') or ''
            ended = await is_market_ended(slug)
            if ended is True:
                info('Market already ended (trades likely settlement/redemption), skipping aggregation')
                for trade in agg['trades']:
                    col = get_user_activity_collection(trade['userAddress'])
                    col.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                separator()
                continue

        proxy_wallet, clob_client = get_next_follow(follow_list)
        try:
            my_positions_data = await fetch_data_async(f'https://data-api.polymarket.com/positions?user={proxy_wallet}')
            user_positions_data = await fetch_data_async(f'https://data-api.polymarket.com/positions?user={agg["userAddress"]}')
            
            my_positions_list = my_positions_data if isinstance(my_positions_data, list) else []
            user_positions_list = user_positions_data if isinstance(user_positions_data, list) else []
            
            my_position = next(
                (p for p in my_positions_list if p.get('conditionId') == agg.get('conditionId')),
                None
            )
            user_position = next(
                (p for p in user_positions_list if p.get('conditionId') == agg.get('conditionId')),
                None
            )
            
            # Get USDC balance for this follow wallet
            my_balance = await get_my_balance_async(proxy_wallet)
            
            # Calculate trader's total portfolio value from positions
            user_balance = sum(pos.get('currentValue', 0) or 0 for pos in user_positions_list)
            
            log_balance(my_balance, user_balance, agg['userAddress'])
            
            # Create a synthetic trade object for postOrder using aggregated values
            synthetic_trade: TradeWithUser = {
                **agg['trades'][0],  # Use first trade as template
                'usdcSize': agg['totalUsdcSize'],
                'price': agg['averagePrice'],
                'side': agg.get('side', 'BUY'),
            }
            
            # Execute the aggregated trade: use 'merge' for SELL so we actually sell our position
            await post_order(
                clob_client,
                'buy' if agg.get('side', 'BUY') == 'BUY' else 'merge',
                my_position,
                user_position,
                synthetic_trade,
                my_balance,
                user_balance,
                agg['userAddress']
            )
            # post_order only marks the synthetic trade's _id (first trade) as bot: True.
            # Explicitly mark every individual trade in this aggregation as fully processed.
            for _t in agg['trades']:
                _col = get_user_activity_collection(_t['userAddress'])
                _col.update_one({'_id': _t['_id']}, {'$set': {'bot': True}})
        except Exception as e:
            error(
                f'Aggregated trade failed for {agg.get("slug") or agg.get("asset", "?")} '
                f'(follow wallet {proxy_wallet[:10]}...): {e}'
            )
            warning('Aggregation marked as processed; will not retry with another wallet.')
            for trade in agg['trades']:
                col = get_user_activity_collection(trade['userAddress'])
                col.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
        separator()


def stop_trade_executor() -> None:
    """Stop the trade executor gracefully"""
    global is_running
    is_running = False
    info('Trade executor shutdown requested...')


async def trade_executor(follow_list: List[FollowEntry]) -> None:
    """Main trade executor function. follow_list: [(proxy_wallet, clob_client), ...]."""
    global _startup_time_sec
    if not follow_list:
        raise ValueError('trade_executor requires at least one follow wallet')
    _startup_time_sec = time.time()
    info(f'Startup time recorded: only trades with API timestamp >= now will be executed')
    success(f'Trade executor ready for {len(USER_ADDRESSES)} trader(s), {len(follow_list)} follow wallet(s)')
    if TRADE_AGGREGATION_ENABLED:
        info(
            f'Trade aggregation enabled: {TRADE_AGGREGATION_WINDOW_SECONDS}s window, '
            f'${TRADE_AGGREGATION_MIN_TOTAL_USD} minimum'
        )
    
    last_check = time.time()
    last_waiting_log = 0.0  # Separate timer for waiting message (throttled for PM2 logs)
    WAITING_LOG_INTERVAL = 30  # Print "waiting" message at most once every 30 seconds
    
    while is_running:
        trades = await read_temp_trades()
        now = time.time()
        
        if TRADE_AGGREGATION_ENABLED:
            # Process with aggregation logic
            if trades:
                clear_line()
                info(f'{len(trades)} new trade{"s" if len(trades) > 1 else ""} detected')
                
                # Add trades to aggregation buffer
                for trade in trades:
                    # Only aggregate BUY trades below minimum threshold
                    if trade.get('side') == 'BUY' and trade.get('usdcSize', 0) < TRADE_AGGREGATION_MIN_TOTAL_USD:
                        info(
                            f"Adding ${trade.get('usdcSize', 0):.2f} {trade.get('side', 'BUY')} trade to aggregation buffer "
                            f"for {trade.get('slug') or trade.get('asset', 'unknown')}"
                        )
                        add_to_aggregation_buffer(trade)
                        # Mark as being processed immediately to prevent re-fetching on next poll cycle.
                        # Without this, read_temp_trades() would return the same trade every 0.3s,
                        # causing it to be added to the buffer repeatedly and inflating the aggregated amount.
                        _agg_col = get_user_activity_collection(trade['userAddress'])
                        _agg_col.update_one({'_id': trade['_id']}, {'$set': {'botExcutedTime': 1}})
                    else:
                        # Execute large trades immediately (not aggregated)
                        clear_line()
                        header('IMMEDIATE TRADE (above threshold)')
                        await do_trading(follow_list, [trade])
                
                last_check = now
                last_waiting_log = now
            
            # Check for ready aggregated trades
            ready_aggregations = get_ready_aggregated_trades()
            if ready_aggregations:
                clear_line()
                header(
                    f"{len(ready_aggregations)} AGGREGATED TRADE{'S' if len(ready_aggregations) > 1 else ''} READY"
                )
                await do_aggregated_trading(follow_list, ready_aggregations)
                last_check = now
                last_waiting_log = now
            
            # Update waiting message (throttled)
            if not trades and not ready_aggregations:
                if now - last_waiting_log >= WAITING_LOG_INTERVAL:
                    buffered_count = len(trade_aggregation_buffer)
                    if buffered_count > 0:
                        waiting(len(USER_ADDRESSES), f'{buffered_count} trade group(s) pending')
                    else:
                        waiting(len(USER_ADDRESSES))
                    last_waiting_log = now
        else:
            # Original non-aggregation logic
            if trades:
                clear_line()
                header(f'{len(trades)} NEW TRADE{"S" if len(trades) > 1 else ""} TO COPY')
                await do_trading(follow_list, trades)
                last_check = now
                last_waiting_log = now
            else:
                # Update waiting message (throttled to avoid PM2 log spam)
                if now - last_waiting_log >= WAITING_LOG_INTERVAL:
                    waiting(len(USER_ADDRESSES))
                    last_waiting_log = now
        
        if not is_running:
            break
        
        await asyncio.sleep(0.3)
    
    info('Trade executor stopped')
