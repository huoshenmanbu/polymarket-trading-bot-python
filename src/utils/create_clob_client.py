"""
Create Polymarket CLOB client.

Note: Order submission (create_market_order, post_order) and API key creation
(create_api_key, derive_api_key) are placeholders. For production you must
implement and audit the full CLOB flow. See SECURITY.md and Polymarket CLOB docs.
"""
from typing import Optional, Dict, Any, List, Tuple
from web3 import Web3
from eth_account import Account
from ..config.env import ENV
from ..utils.logger import info, error


async def is_gnosis_safe(address: str) -> bool:
    """Determines if a wallet is a Gnosis Safe by checking if it has contract code"""
    try:
        w3 = Web3(Web3.HTTPProvider(ENV.RPC_URL))
        # Convert address to checksum format for web3.py
        checksum_address = Web3.to_checksum_address(address)
        code = w3.eth.get_code(checksum_address)
        # If code is not "0x", then it's a contract (likely Gnosis Safe)
        return code != b'0x'
    except Exception as e:
        error(f'Error checking wallet type: {e}')
        return False


class ClobClient:
    """Simplified Polymarket CLOB client wrapper"""
    
    def __init__(
        self,
        host: str,
        chain_id: int,
        wallet: Any,
        api_creds: Optional[Dict[str, Any]] = None,
        signature_type: str = 'EOA',
        proxy_wallet: Optional[str] = None
    ):
        self.host = host.rstrip('/')
        self.chain_id = chain_id
        self.wallet = wallet
        self.api_creds = api_creds or {}
        self.signature_type = signature_type
        self.proxy_wallet = proxy_wallet
        self.api_key = api_creds.get('key') if api_creds else None
        self.api_secret = api_creds.get('secret') if api_creds else None
        self.api_passphrase = api_creds.get('passphrase') if api_creds else None
    
    async def create_api_key(self) -> Dict[str, Any]:
        """Create API key - placeholder, needs implementation"""
        # This would need to call the Polymarket API to create keys
        # For now, return empty dict
        return {}
    
    async def derive_api_key(self) -> Dict[str, Any]:
        """Derive API key - placeholder, needs implementation"""
        # This would need to call the Polymarket API to derive keys
        # For now, return empty dict
        return {}
    
    async def get_order_book(self, token_id: str) -> Dict[str, Any]:
        """Get order book for a token"""
        import httpx
        url = f'{self.host}/book?token_id={token_id}'
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
    
    async def create_market_order(self, order_args: Dict[str, Any]) -> Dict[str, Any]:
        """Create a market order - placeholder, needs full implementation"""
        # This needs to create and sign an order according to Polymarket's format
        # For now, return a placeholder
        return {
            'side': order_args.get('side'),
            'tokenID': order_args.get('tokenID'),
            'amount': order_args.get('amount'),
            'price': order_args.get('price'),
        }
    
    async def post_order(self, signed_order: Dict[str, Any], order_type: str) -> Dict[str, Any]:
        """Post order to Polymarket - placeholder, needs full implementation"""
        # This needs to post the signed order to Polymarket's API
        # For now, return a placeholder response
        return {'success': False, 'error': 'Not implemented - requires full CLOB client implementation'}


async def _create_one_clob_client(
    proxy_wallet: str,
    private_key: str,
    is_proxy_safe: Optional[bool] = None,
) -> ClobClient:
    """Create one CLOB client for a single (proxy_wallet, private_key).
    If is_proxy_safe is provided, skip RPC check (avoids duplicate is_gnosis_safe call).
    """
    chain_id = 137  # Polygon
    host = ENV.CLOB_HTTP_URL
    try:
        account = Account.from_key(private_key)
    except Exception as e:
        error(f'Invalid PRIVATE_KEY for {proxy_wallet[:10]}...: {e}')
        raise ValueError(f'Invalid private key for follow wallet {proxy_wallet[:10]}...') from e

    if is_proxy_safe is None:
        is_proxy_safe = await is_gnosis_safe(proxy_wallet)
    signature_type = 'POLY_GNOSIS_SAFE' if is_proxy_safe else 'EOA'

    clob_client = ClobClient(
        host=host,
        chain_id=chain_id,
        wallet=account,
        signature_type=signature_type,
        proxy_wallet=proxy_wallet if is_proxy_safe else None
    )
    try:
        creds = await clob_client.create_api_key()
        if not creds.get('key'):
            creds = await clob_client.derive_api_key()
    except Exception as e:
        error(f'Failed to create/derive API key for {proxy_wallet[:10]}...: {e}')
        creds = {}
    return ClobClient(
        host=host,
        chain_id=chain_id,
        wallet=account,
        api_creds=creds,
        signature_type=signature_type,
        proxy_wallet=proxy_wallet if is_proxy_safe else None
    )


async def create_clob_clients() -> List[Tuple[str, ClobClient]]:
    """Create one CLOB client per follow wallet. Returns list of (proxy_wallet, clob_client)."""
    if not ENV.PROXY_WALLETS:
        raise ValueError('No follow wallets configured (PROXY_WALLETS or PROXY_WALLET must be set)')
    result: List[Tuple[str, ClobClient]] = []
    for i, (proxy_wallet, private_key) in enumerate(zip(ENV.PROXY_WALLETS, ENV.PRIVATE_KEYS)):
        is_safe = await is_gnosis_safe(proxy_wallet)
        info(
            f'Follow wallet {i + 1}/{len(ENV.PROXY_WALLETS)}: '
            f'{"Gnosis Safe" if is_safe else "EOA"} ({proxy_wallet[:10]}...)'
        )
        client = await _create_one_clob_client(proxy_wallet, private_key, is_proxy_safe=is_safe)
        result.append((proxy_wallet, client))
    return result


async def create_clob_client() -> ClobClient:
    """Create single CLOB client (first follow wallet). For backward compatibility."""
    clients = await create_clob_clients()
    if not clients:
        raise ValueError('No follow wallets configured')
    return clients[0][1]

