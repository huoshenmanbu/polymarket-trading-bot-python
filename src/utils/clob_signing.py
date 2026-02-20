"""
CLOB signing utilities for Polymarket

Handles EIP-712 signing for L1 authentication and HMAC signing for L2 authentication.
"""
import time
import hmac
import hashlib
import base64
import secrets
from typing import Dict, Any, Optional
from eth_utils import keccak
from poly_eip712_structs import make_domain
from py_order_utils.utils import prepend_zx
from web3 import Web3


# Polymarket CLOB constants
CLOB_DOMAIN_NAME = "ClobAuthDomain"
CLOB_VERSION = "1"
CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"

# Polymarket Exchange contract addresses (Polygon mainnet)
EXCHANGE_ADDRESS_STANDARD = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
EXCHANGE_ADDRESS_NEG_RISK = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# Timestamp tolerance: reject timestamps more than 5 minutes in the future or past
TIMESTAMP_TOLERANCE_SECONDS = 300


class ClobAuth:
    """EIP-712 ClobAuth struct for L1 authentication"""
    
    def __init__(self, address: str, timestamp: str, nonce: int, message: str):
        self.address = address
        self.timestamp = timestamp
        self.nonce = nonce
        self.message = message
    
    def signable_bytes(self, domain: Dict[str, Any]) -> bytes:
        """Generate signable bytes for EIP-712 signing"""
        from poly_eip712_structs import make_struct
        
        types = {
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ]
        }
        
        value = {
            "address": self.address,
            "timestamp": self.timestamp,
            "nonce": self.nonce,
            "message": self.message,
        }
        
        try:
            struct = make_struct(types["ClobAuth"], value)
            return struct.signable_bytes(domain)
        except Exception as e:
            # If make_struct fails, raise with helpful error message
            raise RuntimeError(
                f"Failed to create EIP-712 struct: {e}. "
                f"Ensure poly-eip712-structs is installed correctly: pip install poly-eip712-structs"
            ) from e


def get_clob_auth_domain(chain_id: int) -> Dict[str, Any]:
    """Get CLOB authentication domain for EIP-712"""
    return make_domain(name=CLOB_DOMAIN_NAME, version=CLOB_VERSION, chainId=chain_id)


def sign_eip712_auth(
    signer_address: str,
    private_key: str,
    timestamp: int,
    nonce: int,
    chain_id: int = 137
) -> str:
    """
    Sign EIP-712 authentication message for L1 authentication
    
    Args:
        signer_address: The signing address (checksum format)
        private_key: Private key (hex string, with or without 0x prefix)
        timestamp: Unix timestamp (integer)
        nonce: Nonce for API key creation/derivation (integer)
        chain_id: Chain ID (default 137 for Polygon)
    
    Returns:
        Signature string (hex with 0x prefix)
    
    Raises:
        ValueError: If timestamp is unreasonable or parameters are invalid
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct, SignableMessage
    
    # Validate timestamp is within reasonable range
    current_time = int(time.time())
    if abs(timestamp - current_time) > TIMESTAMP_TOLERANCE_SECONDS:
        raise ValueError(
            f"Timestamp {timestamp} is too far from current time {current_time}. "
            f"Check system clock synchronization."
        )
    
    # Validate nonce is non-negative
    if nonce < 0:
        raise ValueError(f"Nonce must be non-negative, got {nonce}")
    
    # Create ClobAuth struct
    clob_auth = ClobAuth(
        address=signer_address,
        timestamp=str(timestamp),
        nonce=nonce,
        message=CLOB_AUTH_MESSAGE
    )
    
    # Get domain
    domain = get_clob_auth_domain(chain_id)
    
    # Generate signable bytes and hash
    signable_bytes = clob_auth.signable_bytes(domain)
    msg_hash = keccak(signable_bytes)
    
    # Normalize private key (ensure 0x prefix for eth_account)
    key_hex = private_key if isinstance(private_key, str) and private_key.startswith('0x') else ('0x' + private_key)
    account = Account.from_key(key_hex)
    
    # Use SignableMessage to sign raw hash (replaces deprecated signHash)
    # version=b'\x00' indicates raw hash signing per EIP-191 specification
    signable_msg = SignableMessage(
        version=b'\x00',
        header=b'',
        body=msg_hash
    )
    signed = account.sign_message(signable_msg)
    
    return prepend_zx(signed.signature.hex())


def sign_hmac_l2(
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: Optional[str] = None
) -> str:
    """
    Create HMAC-SHA256 signature for L2 authentication
    
    Args:
        secret: Base64-encoded API secret
        timestamp: Unix timestamp as string
        method: HTTP method (GET, POST, etc.)
        request_path: API endpoint path
        body: Request body (optional, JSON string)
    
    Returns:
        Base64 URL-safe encoded signature
    
    Raises:
        ValueError: If secret is invalid or parameters are malformed
    """
    # Validate inputs
    if not secret or not secret.strip():
        raise ValueError("API secret cannot be empty")
    if not timestamp:
        raise ValueError("Timestamp is required for HMAC signing")
    if not method:
        raise ValueError("HTTP method is required for HMAC signing")
    if not request_path:
        raise ValueError("Request path is required for HMAC signing")
    
    # Normalize method to uppercase
    method = method.upper().strip()
    if method not in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH'):
        raise ValueError(f"Invalid HTTP method: {method}")
    
    # Decode the base64 secret (add padding if needed for urlsafe_b64decode)
    secret_clean = secret.strip()
    pad = 4 - (len(secret_clean) % 4)
    if pad != 4:
        secret_clean += '=' * pad
    try:
        secret_bytes = base64.urlsafe_b64decode(secret_clean)
    except Exception as e:
        raise ValueError(f"Invalid API secret (base64 decode failed): {e}") from e
    if not secret_bytes:
        raise ValueError("API secret is empty after decode")
    
    # Build message: timestamp + method + requestPath + body
    message = str(timestamp) + method + str(request_path)
    if body:
        message += str(body)
    
    # Create HMAC signature using constant-time comparison internally
    h = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256)
    
    # Return base64 URL-safe encoded signature
    return base64.urlsafe_b64encode(h.digest()).decode("utf-8")


def get_timestamp() -> int:
    """
    Get current Unix timestamp for authentication
    
    Note: Polymarket CLOB API doesn't have a server timestamp endpoint,
    so we use local time. If authentication fails, may need to adjust.
    """
    return int(time.time())


def generate_order_nonce() -> int:
    """
    Generate a unique nonce for orders
    
    Combines timestamp with cryptographically secure random bits to ensure uniqueness
    even under high concurrency. The upper bits are timestamp-based for ordering,
    and lower bits are random for collision resistance.
    
    Returns:
        A unique 64-bit nonce (fits in uint64)
    """
    # Upper 40 bits: timestamp in milliseconds (good for ~34 years)
    # Lower 24 bits: cryptographically secure random (16M possibilities per ms)
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFF  # 40 bits
    random_bits = secrets.randbits(24)  # 24 bits
    return (ts_ms << 24) | random_bits
