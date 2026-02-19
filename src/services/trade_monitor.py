"""
Trade monitor service - ingests trader activity from Polymarket data-api into MongoDB.

Data source: https://data-api.polymarket.com/activity (official API). This ensures
the executor's trade data is auditable and not from an untrusted path.
"""
import asyncio
import time
from typing import List, Dict, Any, Optional
from ..config.env import ENV
from ..models.user_history import get_user_activity_collection
from ..utils.fetch_data import fetch_data_async
from ..utils.logger import info, success, warning, error

USER_ADDRESSES = ENV.USER_ADDRESSES
TOO_OLD_TIMESTAMP_HOURS = ENV.TOO_OLD_TIMESTAMP
FETCH_INTERVAL = ENV.FETCH_INTERVAL
ACTIVITY_API_BASE = 'https://data-api.polymarket.com/activity'
ACTIVITY_PAGE_SIZE = 100

if not USER_ADDRESSES or len(USER_ADDRESSES) == 0:
    raise ValueError('USER_ADDRESSES is not defined or empty')

is_running = True


def _normalize_activity_to_doc(activity: Dict[str, Any], address: str) -> Dict[str, Any]:
    """Map data-api activity to the document shape expected by trade_executor."""
    ts = activity.get('timestamp') or activity.get('transactionHash') or ''
    # Stable _id for dedup: prefer API id, else composite
    doc_id = activity.get('id') or f"{address}_{activity.get('transactionHash', '')}_{ts}"
    return {
        '_id': doc_id,
        'type': 'TRADE',
        'bot': False,
        'botExcutedTime': 0,
        'asset': activity.get('asset') or activity.get('assetId') or '',
        'side': activity.get('side', 'BUY').upper(),
        'usdcSize': float(activity.get('usdcSize') or activity.get('size') or 0),
        'price': float(activity.get('price') or 0),
        'slug': activity.get('slug') or activity.get('market') or '',
        'eventSlug': activity.get('eventSlug') or '',
        'conditionId': activity.get('conditionId') or '',
        'transactionHash': activity.get('transactionHash') or '',
        'timestamp': activity.get('timestamp') or 0,
        'user': address,
    }


def _is_too_old(activity: Dict[str, Any]) -> bool:
    """Ignore trades older than TOO_OLD_TIMESTAMP_HOURS."""
    ts = activity.get('timestamp') or 0
    if not ts:
        return True
    # Support seconds or milliseconds
    if ts > 1e12:
        ts_sec = ts / 1000.0
    else:
        ts_sec = float(ts)
    age_hours = (time.time() - ts_sec) / 3600.0
    return age_hours > TOO_OLD_TIMESTAMP_HOURS


async def fetch_user_activity(address: str) -> List[Dict[str, Any]]:
    """Fetch TRADE activity for one user from Polymarket data-api."""
    try:
        url = f'{ACTIVITY_API_BASE}?user={address}&type=TRADE&limit={ACTIVITY_PAGE_SIZE}&offset=0'
        data = await fetch_data_async(url)
        if not isinstance(data, list):
            return []
        return [a for a in data if a.get('type') == 'TRADE' or 'TRADE' in str(a.get('type', ''))]
    except Exception as e:
        error(f'Trade monitor: fetch activity for {address[:10]}... failed: {e}')
        return []


async def process_trade_activity(activity: Dict[str, Any], address: str) -> bool:
    """
    Process one trade activity: normalize and upsert into user_activity collection.
    Returns True if a new trade was inserted/updated for the executor to pick up.
    """
    if _is_too_old(activity):
        return False
    collection = get_user_activity_collection(address)
    doc = _normalize_activity_to_doc(activity, address)
    # Upsert so we don't duplicate; only new or unchanged docs get bot: False, botExcutedTime: 0
    existing = collection.find_one({'_id': doc['_id']})
    if existing:
        # Already stored; executor may have set botExcutedTime. Do not overwrite.
        return False
    try:
        collection.replace_one({'_id': doc['_id']}, doc, upsert=True)
        return True
    except Exception as e:
        error(f'Trade monitor: failed to upsert activity {doc.get("_id")}: {e}')
        return False


async def poll_and_ingest() -> int:
    """Poll data-api for each user and ingest new trades. Returns total new trades ingested."""
    total_new = 0
    for address in USER_ADDRESSES:
        activities = await fetch_user_activity(address)
        for act in activities:
            if not is_running:
                return total_new
            if await process_trade_activity(act, address):
                total_new += 1
        await asyncio.sleep(0.2)  # Brief delay between users to avoid rate limits
    return total_new


def stop_trade_monitor():
    """Stop the trade monitor loop."""
    global is_running
    is_running = False


async def trade_monitor():
    """Main loop: poll Polymarket data-api at FETCH_INTERVAL and ingest trades into MongoDB."""
    success(f'Trade monitor started (data source: {ACTIVITY_API_BASE})')
    info(f'Polling every {FETCH_INTERVAL}s for {len(USER_ADDRESSES)} trader(s), ignoring trades older than {TOO_OLD_TIMESTAMP_HOURS}h')
    while is_running:
        try:
            n = await poll_and_ingest()
            if n > 0:
                info(f'Trade monitor: ingested {n} new trade(s)')
        except asyncio.CancelledError:
            break
        except Exception as e:
            error(f'Trade monitor poll error: {e}')
        await asyncio.sleep(FETCH_INTERVAL)
    info('Trade monitor stopped')
