"""
Get USDC balance for an address
"""
import asyncio
from web3 import Web3
from ..config.env import ENV


USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    }
]


def _sync_get_balance(address: str) -> float:
    """Synchronous helper - runs in a thread pool to avoid blocking the event loop."""
    w3 = Web3(Web3.HTTPProvider(ENV.RPC_URL))
    checksum_address = Web3.to_checksum_address(address)
    checksum_usdc_address = Web3.to_checksum_address(ENV.USDC_CONTRACT_ADDRESS)
    usdc_contract = w3.eth.contract(address=checksum_usdc_address, abi=USDC_ABI)
    balance_usdc = usdc_contract.functions.balanceOf(checksum_address).call()
    return float(balance_usdc / 10**6)


async def get_my_balance_async(address: str) -> float:
    """Get USDC balance for an address (async, non-blocking)."""
    return await asyncio.to_thread(_sync_get_balance, address)


def get_my_balance(address: str) -> float:
    """Get USDC balance for an address (sync wrapper)"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(get_my_balance_async(address))
        else:
            return loop.run_until_complete(get_my_balance_async(address))
    except RuntimeError:
        return asyncio.run(get_my_balance_async(address))

