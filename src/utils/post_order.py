"""
Post order to Polymarket

Uses the CLOB client (create_clob_client.ClobClient) to create and submit orders.
The real implementation lives in src/utils/create_clob_client.py and src/utils/clob_signing.py.
Before production use, audit the signing and order flow per SECURITY.md.
"""
import asyncio
from typing import Optional, Dict, Any
from ..config.env import ENV
from ..models.user_history import get_user_activity_collection
from ..utils.logger import info, warning, order_result, error
from ..config.copy_strategy import calculate_order_size, get_trade_multiplier

RETRY_LIMIT = ENV.RETRY_LIMIT
COPY_STRATEGY_CONFIG = ENV.COPY_STRATEGY_CONFIG
PREVIEW_MODE = ENV.PREVIEW_MODE

# One-time warning if the CLOB response indicates "not implemented" (e.g. stub client)
_CLOB_PLACEHOLDER_WARNED = False

# Polymarket minimum order sizes (BUY uses COPY_STRATEGY_CONFIG.min_order_size_usd from env)
MIN_ORDER_SIZE_TOKENS = 1.0  # Minimum order size in tokens for SELL/MERGE orders (fallback when book has no min)


def _parse_book_min_order_size(order_book: Dict[str, Any]) -> Optional[float]:
    """
    Parse min_order_size from Polymarket orderbook response.
    BUY: unit is USD; SELL/MERGE: unit is tokens (shares).
    Returns None if missing or invalid so callers can fall back to config-only.
    """
    val = order_book.get('min_order_size') if 'min_order_size' in order_book else order_book.get('minOrderSize')
    if val is None:
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def extract_order_error(response: Any) -> Optional[str]:
    """Extract error message from order response"""
    if not response:
        return None
    
    if isinstance(response, str):
        return response
    
    if isinstance(response, dict):
        # Check direct error
        if 'error' in response:
            error_val = response['error']
            if isinstance(error_val, str):
                return error_val
            if isinstance(error_val, dict):
                if 'error' in error_val:
                    return error_val['error']
                if 'message' in error_val:
                    return error_val['message']
        
        # Check other error fields
        if 'errorMsg' in response:
            return response['errorMsg']
        if 'message' in response:
            return response['message']
    
    return None


def is_insufficient_balance_or_allowance_error(message: Optional[str]) -> bool:
    """Check if error is related to insufficient balance or allowance"""
    if not message:
        return False
    lower = message.lower()
    return 'not enough balance' in lower or 'allowance' in lower


def _warn_if_clob_placeholder(response: Any) -> None:
    """Log a one-time warning if the CLOB response indicates a stub/not-implemented client."""
    global _CLOB_PLACEHOLDER_WARNED
    if _CLOB_PLACEHOLDER_WARNED:
        return
    err = extract_order_error(response)
    if err and 'not implemented' in (err or '').lower() and 'clob' in (err or '').lower():
        _CLOB_PLACEHOLDER_WARNED = True
        error(
            'CLOB returned "not implemented". Ensure you are using the real CLOB client '
            '(create_clob_client.ClobClient) and that create_market_order/post_order are implemented. See SECURITY.md.'
        )


async def post_order(
    clob_client: Any,
    condition: str,
    my_position: Optional[Dict[str, Any]],
    user_position: Optional[Dict[str, Any]],
    trade: Dict[str, Any],
    my_balance: float,
    user_balance: float,
    user_address: str
):
    """Post order to Polymarket"""
    if PREVIEW_MODE:
        info('[PREVIEW MODE] Simulating trade - no real orders will be submitted')
    collection = get_user_activity_collection(user_address)
    
    if condition == 'merge':
        info('Executing MERGE strategy (SELL order)...')
        if not my_position:
            warning('No position to merge/sell')
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
            return
        
        remaining = my_position.get('size', 0)
        
        # Check minimum order size
        if remaining < MIN_ORDER_SIZE_TOKENS:
            warning(f'Position size ({remaining:.2f} tokens) too small to merge - skipping')
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
            return
        
        retry = 0
        abort_due_to_funds = False
        below_market_min = False  # set True when we skip due to market minimum, not retry exhaustion
        
        while remaining > 0 and retry < RETRY_LIMIT:
            # Check minimum token size before each attempt
            if remaining < MIN_ORDER_SIZE_TOKENS:
                info(f'Remaining position ({remaining:.4f} tokens) below minimum - completing sell')
                collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                break

            try:
                order_book = await clob_client.get_order_book(trade['asset'])
                if order_book.get('_orderbook_not_found'):
                    warning(
                        'Order book not found for this token (404). '
                        'Market may be expired, resolved, or delisted - skipping.'
                    )
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    return
                if not order_book.get('bids') or len(order_book['bids']) == 0:
                    warning('No bids available in order book')
                    break
                
                book_min_tokens = _parse_book_min_order_size(order_book)
                effective_min_tokens = max(MIN_ORDER_SIZE_TOKENS, book_min_tokens if book_min_tokens is not None else 0)
                if book_min_tokens is not None and book_min_tokens > MIN_ORDER_SIZE_TOKENS:
                    info(f'Market minimum order size {effective_min_tokens:.2f} tokens (above config {MIN_ORDER_SIZE_TOKENS})')

                if remaining < effective_min_tokens:
                    info(
                        f'Remaining {remaining:.2f} tokens below market minimum ({effective_min_tokens:.2f}) - completing'
                    )
                    below_market_min = True
                    break
                
                max_price_bid = max(order_book['bids'], key=lambda x: float(x['price']))
                
                info(f'Best bid: {max_price_bid["size"]} @ ${max_price_bid["price"]}')
                
                # Use trade['asset'] if available, otherwise fall back to my_position['asset']
                token_id = trade.get('asset') or my_position.get('asset')
                if not token_id:
                    warning('Missing tokenID for SELL order')
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    break
                
                bid_size = float(max_price_bid['size'])
                bid_price = float(max_price_bid['price'])
                sell_amount = remaining if remaining <= bid_size else bid_size

                # If the best bid quantity is below the minimum order size we cannot fill it.
                # Count as a retry (so we don't loop forever) and skip this attempt.
                if sell_amount < MIN_ORDER_SIZE_TOKENS and remaining >= MIN_ORDER_SIZE_TOKENS:
                    retry += 1
                    warning(
                        f'Best bid size ({bid_size:.4f} tokens) is below minimum '
                        f'({MIN_ORDER_SIZE_TOKENS} tokens) — order book too thin '
                        f'(attempt {retry}/{RETRY_LIMIT})'
                    )
                    continue

                order_args = {
                    'side': 'SELL',
                    'tokenID': token_id,
                    'amount': sell_amount,
                    'price': bid_price,
                }
                
                if order_args['amount'] < effective_min_tokens:
                    info(
                        f'Best bid size ({order_args["amount"]:.2f} tokens) below market minimum ({effective_min_tokens:.2f}) - completing'
                    )
                    below_market_min = True
                    break
                
                if PREVIEW_MODE:
                    info(f'[PREVIEW] Would SELL {order_args["amount"]} tokens @ ${order_args["price"]} (not submitted)')
                    remaining -= order_args['amount']
                    continue

                signed_order = await clob_client.create_market_order(order_args)
                resp = await clob_client.post_order(signed_order, 'FOK')
                
                if resp.get('success') is True:
                    retry = 0
                    usdc_received = order_args['amount'] * order_args['price']
                    order_result(True, f'Sold {order_args["amount"]:.2f} tokens at ${order_args["price"]} (received ${usdc_received:.2f})')
                    remaining -= order_args['amount']
                    # Note: Balance will be updated on next check, so we don't track it here
                else:
                    _warn_if_clob_placeholder(resp)
                    error_message = extract_order_error(resp)
                    if is_insufficient_balance_or_allowance_error(error_message):
                        abort_due_to_funds = True
                        warning(f'Order rejected: {error_message or "Insufficient balance or allowance"}')
                        warning('Skipping remaining attempts. Top up funds or check allowance before retrying.')
                        break
                    retry += 1
                    warning(f'Order failed (attempt {retry}/{RETRY_LIMIT}){f" - {error_message}" if error_message else ""}')
            except Exception as e:
                retry += 1
                warning(f'Order error (attempt {retry}/{RETRY_LIMIT}): {e}')
        
        if abort_due_to_funds:
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'bot': True, 'botExcutedTime': RETRY_LIMIT}}
            )
            return
        
        if not below_market_min and retry >= RETRY_LIMIT:
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'bot': True, 'botExcutedTime': retry}}
            )
        else:
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
    
    elif condition == 'buy':
        info('Executing BUY strategy...')
        
        info(f'Your balance: ${my_balance:.2f}')
        info(f'Trader bought: ${trade.get("usdcSize", 0):.2f}')
        
        # Get current position size for position limit checks
        current_position_value = (my_position.get('size', 0) * my_position.get('avgPrice', 0)) if my_position else 0
        
        # Use new copy strategy system
        order_calc = calculate_order_size(
            COPY_STRATEGY_CONFIG,
            trade.get('usdcSize', 0),
            my_balance,
            current_position_value
        )
        
        # Log the calculation reasoning
        info(f'{order_calc.reasoning}')
        
        # Check if order should be executed
        if order_calc.final_amount == 0:
            warning(f'Cannot execute: {order_calc.reasoning}')
            if order_calc.below_minimum:
                warning('Increase COPY_SIZE or wait for larger trades')
            collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
            return
        
        remaining = order_calc.final_amount
        available_balance = my_balance  # Track remaining balance after orders
        
        retry = 0
        abort_due_to_funds = False
        total_bought_tokens = 0  # Track total tokens bought for this trade
        empty_asks_retried = False  # Retry once after short delay when book is empty (thin book / race)

        while remaining > 0 and retry < RETRY_LIMIT:
            try:
                order_book = await clob_client.get_order_book(trade['asset'])
                if order_book.get('_orderbook_not_found'):
                    warning(
                        'Order book not found for this token (404). '
                        'Market may be expired, resolved, or delisted - skipping.'
                    )
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    return
                if not order_book.get('asks') or len(order_book['asks']) == 0:
                    if not empty_asks_retried:
                        info('No asks available in order book; retrying once in 1.5s (thin book / race)')
                        empty_asks_retried = True
                        await asyncio.sleep(1.5)
                        continue
                    warning('No asks available in order book')
                    collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})
                    break
                
                book_min_usd = _parse_book_min_order_size(order_book)
                config_min = COPY_STRATEGY_CONFIG.min_order_size_usd
                min_usd = max(config_min, book_min_usd if book_min_usd is not None else 0)
                if book_min_usd is not None and book_min_usd > config_min:
                    info(f'Market minimum order size ${book_min_usd:.2f} (using max of config ${config_min:.2f})')
                
                min_price_ask = min(order_book['asks'], key=lambda x: float(x['price']))
                
                info(f'Best ask: {min_price_ask["size"]} @ ${min_price_ask["price"]}')
                
                if remaining < min_usd:
                    info(f'Remaining amount (${remaining:.2f}) below minimum (${min_usd}) - completing trade')
                    collection.update_one(
                        {'_id': trade['_id']},
                        {'$set': {'bot': True, 'myBoughtSize': total_bought_tokens}}
                    )
                    break
                
                max_order_size = float(min_price_ask['size']) * float(min_price_ask['price'])
                order_size = min(remaining, max_order_size)
                
                # Ensure order size meets minimum (config and/or market min_order_size)
                if order_size < min_usd:
                    info(f'Order size (${order_size:.2f}) below minimum (${min_usd}) - completing trade')
                    collection.update_one(
                        {'_id': trade['_id']},
                        {'$set': {'bot': True, 'myBoughtSize': total_bought_tokens}}
                    )
                    break
                
                # Check if balance is sufficient for the order
                if available_balance < order_size:
                    warning(f'Insufficient balance: Need ${order_size:.2f} but only have ${available_balance:.2f}')
                    abort_due_to_funds = True
                    break
                
                order_args = {
                    'side': 'BUY',
                    'tokenID': trade['asset'],
                    'amount': order_size,
                    'price': float(min_price_ask['price']),
                }
                
                info(f'Creating order: ${order_size:.2f} @ ${min_price_ask["price"]} (Balance: ${available_balance:.2f})')

                if PREVIEW_MODE:
                    tokens_bought = order_args['amount'] / order_args['price']
                    total_bought_tokens += tokens_bought
                    info(f'[PREVIEW] Would BUY ${order_args["amount"]:.2f} @ ${order_args["price"]} ({tokens_bought:.2f} tokens) (not submitted)')
                    remaining -= order_args['amount']
                    available_balance -= order_args['amount']
                    continue

                signed_order = await clob_client.create_market_order(order_args)
                resp = await clob_client.post_order(signed_order, 'FOK')
                
                if resp.get('success') is True:
                    retry = 0
                    tokens_bought = order_args['amount'] / order_args['price']
                    total_bought_tokens += tokens_bought
                    order_result(
                        True,
                        f'Bought ${order_args["amount"]:.2f} at ${order_args["price"]} ({tokens_bought:.2f} tokens)'
                    )
                    remaining -= order_args['amount']
                    # Update balance after successful order
                    available_balance -= order_args['amount']
                else:
                    _warn_if_clob_placeholder(resp)
                    error_message = extract_order_error(resp)
                    if is_insufficient_balance_or_allowance_error(error_message):
                        abort_due_to_funds = True
                        warning(f'Order rejected: {error_message or "Insufficient balance or allowance"}')
                        warning('Skipping remaining attempts. Top up funds or check allowance before retrying.')
                        break
                    retry += 1
                    warning(f'Order failed (attempt {retry}/{RETRY_LIMIT}){f" - {error_message}" if error_message else ""}')
            except Exception as e:
                retry += 1
                warning(f'Order error (attempt {retry}/{RETRY_LIMIT}): {e}')
        
        if abort_due_to_funds:
            update_fields: dict = {'bot': True, 'botExcutedTime': retry}
            if total_bought_tokens > 0:
                update_fields['myBoughtSize'] = total_bought_tokens
            collection.update_one({'_id': trade['_id']}, {'$set': update_fields})
            return
        
        if retry >= RETRY_LIMIT:
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'bot': True, 'botExcutedTime': retry}}
            )
        else:
            collection.update_one(
                {'_id': trade['_id']},
                {'$set': {'bot': True, 'myBoughtSize': total_bought_tokens}}
            )
    
    elif condition == 'sell':
        # SELL strategy - similar to merge but different logic
        info('Executing SELL strategy...')
        # Implementation similar to merge but for selling positions
        # This would be implemented based on the full TypeScript version
        collection.update_one({'_id': trade['_id']}, {'$set': {'bot': True}})

