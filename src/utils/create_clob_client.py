"""
Create Polymarket CLOB client.

This module implements the full CLOB client functionality including:
- L1 authentication (EIP-712 signing) for API key creation/derivation
- L2 authentication (HMAC-SHA256) for order submission
- Order creation and signing using py-order-utils
- Order submission to Polymarket CLOB API

For production use, ensure you have:
1. Installed required dependencies: poly-eip712-structs, py-order-utils
2. Properly configured API credentials
3. Audited the signing and order submission flow

See SECURITY.md and Polymarket CLOB docs for more information.
"""
import json
import secrets
import time
import httpx
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any, List, Tuple
from web3 import Web3
from eth_account import Account
from ..config.env import ENV
from ..utils.logger import info, error, warning
from ..utils.clob_signing import (
    sign_eip712_auth,
    sign_hmac_l2,
    get_timestamp,
    generate_order_nonce,
    EXCHANGE_ADDRESS_STANDARD,
)


def _order_dict_to_camel(d: Dict[str, Any]) -> Dict[str, Any]:
    """Convert order dict keys to camelCase for CLOB API (e.g. token_id -> tokenID)."""
    snake_to_camel = {
        'token_id': 'tokenID',
        'fee_rate_bps': 'feeRateBps',
        'signature_type': 'signatureType',
    }
    out: Dict[str, Any] = {}
    for k, v in d.items():
        key = snake_to_camel.get(k, k)
        out[key] = v
    return out


async def is_gnosis_safe(address: str) -> bool:
    """
    Determines if a wallet is a Gnosis Safe (or other contract) by checking if it has contract code.
    
    Note: This is a heuristic - it detects any contract, not specifically Gnosis Safe.
    For Polymarket, proxy wallets that are contracts are typically Gnosis Safes.
    """
    try:
        w3 = Web3(Web3.HTTPProvider(ENV.RPC_URL))
        # Convert address to checksum format for web3.py
        checksum_address = Web3.to_checksum_address(address)
        code = w3.eth.get_code(checksum_address)
        # EOA returns empty bytes b'' or HexBytes('0x')
        # Contract returns actual bytecode
        # Check both cases for compatibility with different web3 versions
        if code is None:
            return False
        if isinstance(code, bytes):
            return len(code) > 0 and code != b''
        # HexBytes or hex string
        code_hex = code.hex() if hasattr(code, 'hex') else str(code)
        return code_hex not in ('0x', '', '0x0')
    except Exception as e:
        error(f'Error checking wallet type for {address[:10]}...: {e}')
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
        proxy_wallet: Optional[str] = None,
        exchange_address: Optional[str] = None,
        funder: Optional[str] = None,
        eoa_address: Optional[str] = None,
        signature_type_int: Optional[int] = None
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
        
        # New attributes for order creation
        self.exchange_address = exchange_address or EXCHANGE_ADDRESS_STANDARD
        self.funder = funder
        self.eoa_address = eoa_address or wallet.address if hasattr(wallet, 'address') else None
        self.signature_type_int = signature_type_int or self._signature_type_to_int(signature_type)
        
        # Store private key for signing (needed for API key creation and order signing)
        self._private_key = None  # Will be set if needed
    
    def _signature_type_to_int(self, signature_type: str) -> int:
        """Convert signature type string to integer"""
        mapping = {
            'EOA': 0,
            'POLY_PROXY': 1,
            'POLY_GNOSIS_SAFE': 2,
            'GNOSIS_SAFE': 2
        }
        return mapping.get(signature_type.upper(), 0)
    
    def _get_signer_address(self) -> str:
        """Get signer address in checksum format"""
        if self.eoa_address:
            return Web3.to_checksum_address(self.eoa_address)
        return Web3.to_checksum_address(self.wallet.address)
    
    def _get_private_key(self) -> str:
        """
        Get private key for signing (hex string, no 0x prefix for py-order-utils compatibility).
        
        Security note: This method should only be called internally for signing operations.
        Never log or expose the return value.
        """
        raw = None
        if self._private_key:
            raw = self._private_key
        elif hasattr(self.wallet, 'key'):
            raw = self.wallet.key.hex()
        if not raw:
            raise ValueError("Private key not available for signing. Ensure wallet is properly initialized.")
        # Normalize: strip 0x prefix for consistent use with Signer/Account
        key = raw[2:] if isinstance(raw, str) and raw.startswith('0x') else raw
        # Basic validation: should be 64 hex characters
        if len(key) != 64 or not all(c in '0123456789abcdefABCDEF' for c in key):
            raise ValueError("Invalid private key format (expected 64 hex characters)")
        return key
    
    async def create_api_key(self, nonce: Optional[int] = None) -> Dict[str, Any]:
        """
        Create API key using L1 authentication
        
        Args:
            nonce: Nonce for API key creation (default: random)
        
        Returns:
            Dictionary with apiKey, secret, and passphrase
        """
        if nonce is None:
            nonce = 0  # Default nonce=0 so derive_api_key(nonce=0) can recover credentials after restart
        
        try:
            signer_address = self._get_signer_address()
            private_key = self._get_private_key()
            timestamp = get_timestamp()
            
            # Sign EIP-712 authentication message
            signature = sign_eip712_auth(
                signer_address=signer_address,
                private_key=private_key,
                timestamp=timestamp,
                nonce=nonce,
                chain_id=self.chain_id
            )
            
            # Build L1 headers
            headers = {
                'POLY_ADDRESS': signer_address,
                'POLY_SIGNATURE': signature,
                'POLY_TIMESTAMP': str(timestamp),
                'POLY_NONCE': str(nonce),
                'Content-Type': 'application/json'
            }
            
            # POST request to create API key
            url = f'{self.host}/auth/api-key'
            timeout = httpx.Timeout(ENV.REQUEST_TIMEOUT_MS / 1000.0)
            
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers)
                response.raise_for_status()
                result = response.json()
                
                api_key_val = result.get('apiKey')
                secret_val = result.get('secret')
                passphrase_val = result.get('passphrase')
                if not api_key_val or not secret_val or not passphrase_val:
                    error('Create API key response missing apiKey, secret, or passphrase')
                    return {}
                
                # Store credentials
                self.api_creds = {'key': api_key_val, 'secret': secret_val, 'passphrase': passphrase_val}
                self.api_key = api_key_val
                self.api_secret = secret_val
                self.api_passphrase = passphrase_val
                
                info(f'Created API key for {signer_address[:10]}...')
                return self.api_creds
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                error(f'Authentication failed: Invalid signature or timestamp. Check your private key and system time.')
            elif e.response.status_code == 400:
                error_msg = e.response.text
                if 'nonce' in error_msg.lower():
                    error(f'Nonce already used. Try deriving existing API key instead.')
                else:
                    error(f'Bad request: {error_msg}')
            else:
                error(f'Failed to create API key: HTTP {e.response.status_code} - {e.response.text}')
            return {}
        except httpx.TimeoutException:
            error('Request timeout while creating API key. Check network connection.')
            return {}
        except httpx.NetworkError as e:
            error(f'Network error while creating API key: {e}')
            return {}
        except Exception as e:
            error(f'Failed to create API key: {e}')
            return {}
    
    async def derive_api_key(self, nonce: int = 0) -> Dict[str, Any]:
        """
        Derive existing API key using L1 authentication
        
        Args:
            nonce: Nonce used when creating the API key (default: 0)
        
        Returns:
            Dictionary with apiKey, secret, and passphrase, or empty dict if failed
        """
        try:
            signer_address = self._get_signer_address()
            private_key = self._get_private_key()
            timestamp = get_timestamp()
            
            # Sign EIP-712 authentication message
            signature = sign_eip712_auth(
                signer_address=signer_address,
                private_key=private_key,
                timestamp=timestamp,
                nonce=nonce,
                chain_id=self.chain_id
            )
            
            # Build L1 headers
            headers = {
                'POLY_ADDRESS': signer_address,
                'POLY_SIGNATURE': signature,
                'POLY_TIMESTAMP': str(timestamp),
                'POLY_NONCE': str(nonce),
            }
            
            # GET request to derive API key
            url = f'{self.host}/auth/derive-api-key'
            timeout = httpx.Timeout(ENV.REQUEST_TIMEOUT_MS / 1000.0)
            
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                result = response.json()
                
                api_key_val = result.get('apiKey')
                secret_val = result.get('secret')
                passphrase_val = result.get('passphrase')
                if not api_key_val or not secret_val or not passphrase_val:
                    error('Derive API key response missing apiKey, secret, or passphrase')
                    return {}
                
                # Store credentials
                self.api_creds = {'key': api_key_val, 'secret': secret_val, 'passphrase': passphrase_val}
                self.api_key = api_key_val
                self.api_secret = secret_val
                self.api_passphrase = passphrase_val
                
                info(f'Derived API key for {signer_address[:10]}...')
                return self.api_creds
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # API key doesn't exist for this nonce - this is expected
                return {}
            elif e.response.status_code == 401:
                error(f'Authentication failed: Invalid signature or timestamp. Check your private key and system time.')
            elif e.response.status_code == 400:
                error(f'Bad request: {e.response.text}')
            else:
                error(f'Failed to derive API key: HTTP {e.response.status_code} - {e.response.text}')
            return {}
        except httpx.TimeoutException:
            error('Request timeout while deriving API key. Check network connection.')
            return {}
        except httpx.NetworkError as e:
            error(f'Network error while deriving API key: {e}')
            return {}
        except Exception as e:
            error(f'Failed to derive API key: {e}')
            return {}
    
    async def create_or_derive_api_key(self, max_retries: int = 2) -> Dict[str, Any]:
        """
        Convenience method: try to derive API key first, then create if it doesn't exist.
        Includes retry logic for transient failures.
        
        Args:
            max_retries: Maximum number of retry attempts for transient failures
        
        Returns:
            Dictionary with apiKey, secret, and passphrase, or empty dict if all attempts fail
        """
        import asyncio
        
        for attempt in range(max_retries + 1):
            # Try to derive with nonce=0 first (standard nonce for first-time create)
            creds = await self.derive_api_key(nonce=0)
            if creds.get('key'):
                return creds
            
            # If derivation failed (404), create new API key with nonce=0
            creds = await self.create_api_key(nonce=0)
            if creds.get('key'):
                return creds
            
            # If this isn't the last attempt, wait before retrying
            if attempt < max_retries:
                wait_time = (attempt + 1) * 2  # Exponential backoff: 2s, 4s
                warning(f'API key creation failed, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries + 1})...')
                await asyncio.sleep(wait_time)
        
        error('Failed to create or derive API key after all retry attempts')
        return {}
    
    def _build_l2_headers(self, method: str, path: str, body: Optional[str] = None) -> Dict[str, str]:
        """
        Build L2 authentication headers for API requests
        
        Args:
            method: HTTP method (GET, POST, etc.)
            path: Request path
            body: Request body (optional)
        
        Returns:
            Dictionary with L2 headers
        """
        if not self.api_key or not self.api_secret or not self.api_passphrase:
            raise ValueError("API credentials not available. Call create_or_derive_api_key() first.")
        
        timestamp = str(get_timestamp())
        # L2 POLY_ADDRESS: use funder for proxy wallets (type 1/2), signer for EOA (type 0)
        # API credentials are associated with the funder (Polymarket profile) for proxy types
        if self.signature_type_int in (1, 2) and self.funder:
            poly_address = Web3.to_checksum_address(self.funder)
        else:
            poly_address = self._get_signer_address()
        
        # Create HMAC signature
        signature = sign_hmac_l2(
            secret=self.api_secret,
            timestamp=timestamp,
            method=method,
            request_path=path,
            body=body
        )
        
        return {
            'POLY_ADDRESS': poly_address,
            'POLY_SIGNATURE': signature,
            'POLY_TIMESTAMP': timestamp,
            'POLY_API_KEY': self.api_key,
            'POLY_PASSPHRASE': self.api_passphrase,
            'Content-Type': 'application/json'
        }
    
    async def get_order_book(self, token_id: str) -> Dict[str, Any]:
        """Get order book for a token"""
        url = f'{self.host}/book?token_id={token_id}'
        timeout = httpx.Timeout(ENV.REQUEST_TIMEOUT_MS / 1000.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
    
    async def create_market_order(self, order_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create and sign a market order using py-order-utils
        
        Args:
            order_args: Dictionary containing:
                - side: "BUY" or "SELL"
                - tokenID: Token identifier
                - amount: Order size (USD for BUY, tokens for SELL)
                - price: Order price (must be between 0 and 1 exclusive for prediction markets)
        
        Returns:
            Signed order dictionary ready for submission
        
        Raises:
            ValueError: If order parameters are invalid
            ImportError: If py-order-utils is not installed
        """
        try:
            from py_order_utils.builders import OrderBuilder
            from py_order_utils.signer import Signer
            from py_order_utils.model import OrderData, BUY, SELL
            
            # Extract order parameters (preserve string for precision; validate numerically)
            side_str = order_args.get('side', 'BUY').upper()
            if side_str not in ('BUY', 'SELL'):
                raise ValueError(f"Invalid side: {side_str}. Must be 'BUY' or 'SELL'.")
            
            token_id = str(order_args.get('tokenID', '')).strip()
            amount_raw = order_args.get('amount', 0.0)
            price_raw = order_args.get('price', 0.0)
            
            # Validate numerically; keep string form for API to avoid float precision issues
            try:
                amount_val = float(amount_raw) if not isinstance(amount_raw, (int, float)) else float(amount_raw)
                price_val = float(price_raw) if not isinstance(price_raw, (int, float)) else float(price_raw)
            except (ValueError, TypeError) as e:
                raise ValueError(f"Invalid numeric value for amount or price: {e}") from e
            
            size_str = str(amount_raw).strip() if isinstance(amount_raw, str) else str(amount_val)
            price_str = str(price_raw).strip() if isinstance(price_raw, str) else str(price_val)
            
            if not token_id:
                raise ValueError("tokenID is required")
            if amount_val <= 0:
                raise ValueError(f"amount must be positive, got {amount_val}")
            if price_val <= 0 or price_val >= 1:
                raise ValueError(f"price must be between 0 and 1 (exclusive) for prediction markets, got {price_val}")
            if price_val < 0.000001:  # Prevent division by very small numbers that could cause overflow
                raise ValueError(f"price too small, got {price_val}. Minimum price is 0.000001")
            
            # Additional validation for SELL orders: ensure amount is reasonable
            # For SELL orders, amount is token quantity, which should be at least 1 token
            if side == SELL and amount_val < 1.0:
                raise ValueError(f"For SELL orders, amount (token quantity) must be at least 1.0, got {amount_val}")
            
            # Convert side to constant
            side = BUY if side_str == 'BUY' else SELL
            
            # Get addresses in checksum format (must be set by client initialization)
            maker_raw = self.funder or self.eoa_address or (getattr(self.wallet, 'address', None) if self.wallet else None)
            signer_raw = self.eoa_address or (getattr(self.wallet, 'address', None) if self.wallet else None)
            if not maker_raw or not signer_raw:
                raise ValueError("Client missing funder or eoa_address. Ensure CLOB client was created via create_clob_clients().")
            maker_address = Web3.to_checksum_address(maker_raw)
            signer_address = Web3.to_checksum_address(signer_raw)
            
            # Generate order parameters
            order_nonce = generate_order_nonce()
            expiration = int(time.time()) + (30 * 24 * 60 * 60)  # 30 days from now
            fee_rate_bps = 0  # Default fee rate
            
            # Calculate makerAmount and takerAmount in smallest units (6 decimals for USDC and tokens)
            # For BUY orders:
            # - takerAmount: USDC amount we're paying (in smallest units, 6 decimals)
            # - makerAmount: Token amount we're receiving (in smallest units, 6 decimals)
            # For SELL orders:
            # - makerAmount: Token amount we're selling (in smallest units, 6 decimals)
            # - takerAmount: USDC amount we're receiving (in smallest units, 6 decimals)
            # For prediction markets: amount is USDC, price is token price (0-1)
            USDC_DECIMALS = 6
            TOKEN_DECIMALS = 6
            
            # Use Decimal for precise calculations to avoid floating point errors
            amount_decimal = Decimal(str(amount_val))
            price_decimal = Decimal(str(price_val))
            
            if side == BUY:
                # BUY order: we pay USDC, receive tokens
                taker_amount_usdc = amount_decimal  # USDC amount we're paying
                maker_amount_tokens = amount_decimal / price_decimal  # Token amount we're receiving
            else:
                # SELL order: we pay tokens, receive USDC
                # For SELL, amount is token quantity, price is still token price
                # USDC received = token_amount * price
                maker_amount_tokens = amount_decimal  # Token amount we're selling
                taker_amount_usdc = amount_decimal * price_decimal  # USDC amount we're receiving
            
            # Convert to smallest units (multiply by 10^decimals) and round down to avoid overflow
            # Use Decimal arithmetic to avoid precision issues
            taker_amount_int = int((taker_amount_usdc * Decimal(10 ** USDC_DECIMALS)).quantize(Decimal('1'), rounding=ROUND_DOWN))
            maker_amount_int = int((maker_amount_tokens * Decimal(10 ** TOKEN_DECIMALS)).quantize(Decimal('1'), rounding=ROUND_DOWN))
            
            # Validate amounts are positive and within reasonable bounds
            if taker_amount_int <= 0:
                raise ValueError(f"Calculated takerAmount must be positive, got {taker_amount_int}")
            if maker_amount_int <= 0:
                raise ValueError(f"Calculated makerAmount must be positive, got {maker_amount_int}")
            
            # Check for potential overflow (uint256 max is 2^256 - 1, but we use smaller limits for safety)
            MAX_UINT256 = Decimal(2**256 - 1)
            if taker_amount_int > MAX_UINT256 or maker_amount_int > MAX_UINT256:
                raise ValueError(f"Order amounts too large: takerAmount={taker_amount_int}, makerAmount={maker_amount_int}")
            
            taker_amount_str = str(taker_amount_int)
            maker_amount_str = str(maker_amount_int)
            
            # Get private key for signing (py-order-utils Signer expects 0x prefix)
            private_key = self._get_private_key()
            signer_key = private_key if private_key.startswith('0x') else ('0x' + private_key)
            
            # Create Signer
            signer = Signer(signer_key)
            
            # Create OrderBuilder
            builder = OrderBuilder(self.exchange_address, self.chain_id, signer)
            
            # Build OrderData (use camelCase parameter names and string values)
            order_data = OrderData(
                maker=maker_address,
                taker='0x0000000000000000000000000000000000000000',  # Zero address for open orders
                tokenId=token_id,
                makerAmount=maker_amount_str,
                takerAmount=taker_amount_str,
                side=side,
                feeRateBps=str(fee_rate_bps),
                nonce=str(order_nonce),
                signer=signer_address,
                expiration=str(expiration),
                signatureType=self.signature_type_int
            )
            
            # Build and sign order
            signed_order = builder.build_signed_order(order_data)
            
            # Convert to dictionary; CLOB API expects camelCase keys
            # py-order-utils SignedOrder.dict() returns a dict with all order fields + signature
            # The dict already has camelCase keys, so we may not need conversion, but keep it for safety
            if hasattr(signed_order, 'dict'):
                raw = signed_order.dict()
            elif hasattr(signed_order, 'order') and hasattr(signed_order, 'signature'):
                # Fallback: extract order dict and add signature
                order_dict = signed_order.order if isinstance(signed_order.order, dict) else signed_order.order.__dict__
                raw = dict(order_dict) if isinstance(order_dict, dict) else order_dict
                raw['signature'] = signed_order.signature
            else:
                # This should not happen with py-order-utils, but handle gracefully
                error('Warning: Unexpected signed_order format, using fallback construction')
                raw = {
                    'tokenId': token_id,
                    'makerAmount': maker_amount_str,
                    'takerAmount': taker_amount_str,
                    'side': side,  # BUY=0, SELL=1
                    'expiration': str(expiration),
                    'nonce': str(order_nonce),
                    'maker': maker_address,
                    'taker': '0x0000000000000000000000000000000000000000',
                    'signer': signer_address,
                    'feeRateBps': str(fee_rate_bps),
                    'signatureType': self.signature_type_int,
                    'signature': getattr(signed_order, 'signature', '')
                }
            
            # Ensure signature is present (should always be present from py-order-utils)
            if 'signature' not in raw or not raw.get('signature'):
                error('Warning: Missing signature in signed order')
                raw['signature'] = getattr(signed_order, 'signature', '')
            
            # Convert to camelCase (py-order-utils already uses camelCase, but ensure consistency)
            return _order_dict_to_camel(raw)
                
        except ImportError as e:
            error_msg = f'Failed to import py-order-utils: {e}. Install with: pip install py-order-utils'
            error(error_msg)
            raise ImportError(error_msg) from e
        except ValueError as e:
            error(f'Invalid order parameters: {e}')
            raise
        except AttributeError as e:
            error(f'Missing required attribute: {e}. Ensure client is properly initialized.')
            raise
        except Exception as e:
            error(f'Failed to create market order: {e}')
            raise
    
    async def post_order(self, signed_order: Dict[str, Any], order_type: str) -> Dict[str, Any]:
        """
        Post signed order to Polymarket CLOB API using L2 authentication
        
        Args:
            signed_order: Signed order dictionary from create_market_order
            order_type: Order type ("FOK", "IOC", "GTC", "GTD")
        
        Returns:
            Response dictionary with success status and order info or error
        """
        try:
            # Ensure API credentials are available
            if not self.api_key or not self.api_secret or not self.api_passphrase:
                warning('API credentials not available, attempting to create/derive...')
                await self.create_or_derive_api_key()
                if not self.api_key:
                    return {'success': False, 'error': 'Failed to obtain API credentials'}
            
            # Validate order type
            order_type_upper = order_type.upper().strip()
            if order_type_upper not in ('FOK', 'IOC', 'GTC', 'GTD'):
                return {'success': False, 'error': f'Invalid order type: {order_type}. Use FOK, IOC, GTC, or GTD.'}
            
            # Build request path and body (default=str for non-JSON types)
            path = f'/order?orderType={order_type_upper}'
            body = json.dumps(signed_order, default=str)
            
            # Build L2 headers
            headers = self._build_l2_headers('POST', path, body)
            
            # POST request
            url = f'{self.host}{path}'
            timeout = httpx.Timeout(ENV.REQUEST_TIMEOUT_MS / 1000.0)
            
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, content=body)
                
                # Check response status
                if 200 <= response.status_code < 300:
                    try:
                        result = response.json()
                        return {'success': True, **result}
                    except Exception:
                        return {'success': True, 'message': 'Order submitted successfully'}
                else:
                    # Parse error response
                    try:
                        error_data = response.json()
                        error_msg = error_data.get('error') or error_data.get('message') or error_data.get('errorMsg') or response.text
                    except Exception:
                        error_msg = response.text or f'HTTP {response.status_code}'
                    
                    return {'success': False, 'error': error_msg}
                    
        except httpx.HTTPStatusError as e:
            try:
                error_data = e.response.json()
                error_msg = error_data.get('error') or error_data.get('message') or error_data.get('errorMsg') or e.response.text
            except Exception:
                error_msg = e.response.text or f'HTTP {e.response.status_code}'
            
            # Log specific error types
            if e.response.status_code == 401:
                error('Authentication failed: Invalid API credentials or signature')
            elif e.response.status_code == 400:
                error(f'Invalid order: {error_msg}')
            elif e.response.status_code == 403:
                error('Forbidden: Check API key permissions')
            elif e.response.status_code == 429:
                error('Rate limited: Too many requests')
            else:
                error(f'Order submission failed: HTTP {e.response.status_code} - {error_msg}')
            
            return {'success': False, 'error': error_msg}
        except httpx.TimeoutException:
            error('Request timeout while posting order. Check network connection.')
            return {'success': False, 'error': 'Request timeout'}
        except httpx.NetworkError as e:
            error(f'Network error while posting order: {e}')
            return {'success': False, 'error': f'Network error: {str(e)}'}
        except ValueError as e:
            err_str = str(e)
            if 'API credentials' in err_str:
                error('API credentials not available. Call create_or_derive_api_key() first.')
            elif 'Invalid API secret' in err_str or 'base64' in err_str.lower():
                error('Invalid API secret (check credentials). Try create_or_derive_api_key() again.')
            else:
                error(f'Invalid request: {e}')
            return {'success': False, 'error': err_str}
        except Exception as e:
            error(f'Failed to post order: {e}')
            return {'success': False, 'error': str(e)}


async def _create_one_clob_client(
    proxy_wallet: str,
    private_key: str,
    is_proxy_safe: Optional[bool] = None,
) -> ClobClient:
    """
    Create one CLOB client for a single (proxy_wallet, private_key).
    If is_proxy_safe is provided, skip RPC check (avoids duplicate is_gnosis_safe call).
    """
    chain_id = 137  # Polygon
    host = ENV.CLOB_HTTP_URL
    
    try:
        account = Account.from_key(private_key)
    except Exception as e:
        # Do not log private key details - only indicate which wallet failed
        error(f'Invalid private key format for follow wallet {proxy_wallet[:10]}...')
        raise ValueError(f'Invalid private key for follow wallet {proxy_wallet[:10]}...') from e

    # Get EOA address
    eoa_address = Web3.to_checksum_address(account.address)
    proxy_wallet_checksum = Web3.to_checksum_address(proxy_wallet)
    
    # Determine if proxy_wallet is actually EOA or a contract
    if is_proxy_safe is None:
        is_proxy_safe = await is_gnosis_safe(proxy_wallet)
    
    # Check if proxy_wallet is the same as EOA (user using EOA directly)
    is_eoa_mode = proxy_wallet_checksum.lower() == eoa_address.lower()
    
    # Determine signature type and addresses
    # Polymarket signature types:
    #   0 = EOA (direct signing from EOA wallet)
    #   1 = POLY_PROXY (Polymarket proxy wallet - approved operator)
    #   2 = POLY_GNOSIS_SAFE (Gnosis Safe multisig)
    if is_eoa_mode:
        # User is using EOA directly - no proxy
        signature_type = 'EOA'
        signature_type_int = 0
        funder = eoa_address
        maker = eoa_address
        signer = eoa_address
        proxy_wallet_for_client = None
    elif is_proxy_safe:
        # User is using Gnosis Safe proxy (contract with code)
        signature_type = 'POLY_GNOSIS_SAFE'
        signature_type_int = 2
        funder = proxy_wallet_checksum
        maker = proxy_wallet_checksum
        signer = eoa_address
        proxy_wallet_for_client = proxy_wallet_checksum
    else:
        # proxy_wallet is different from EOA but not a contract
        # This means user has a Polymarket proxy wallet (type 1)
        # The proxy_wallet is the Polymarket profile address, EOA is the approved operator
        signature_type = 'POLY_PROXY'
        signature_type_int = 1
        funder = proxy_wallet_checksum
        maker = proxy_wallet_checksum
        signer = eoa_address
        proxy_wallet_for_client = proxy_wallet_checksum

    # Create initial client (without API creds)
    clob_client = ClobClient(
        host=host,
        chain_id=chain_id,
        wallet=account,
        signature_type=signature_type,
        proxy_wallet=proxy_wallet_for_client,
        exchange_address=EXCHANGE_ADDRESS_STANDARD,
        funder=funder,
        eoa_address=eoa_address,
        signature_type_int=signature_type_int
    )
    
    # Store private key for signing
    clob_client._private_key = private_key
    
    # Get API credentials
    try:
        creds = await clob_client.create_or_derive_api_key()
        if not creds.get('key'):
            warning(f'Failed to obtain API credentials for {proxy_wallet[:10]}...')
    except Exception as e:
        error(f'Failed to create/derive API key for {proxy_wallet[:10]}...: {e}')
        creds = {}
    
    # Create final client with API credentials
    final_client = ClobClient(
        host=host,
        chain_id=chain_id,
        wallet=account,
        api_creds=creds,
        signature_type=signature_type,
        proxy_wallet=proxy_wallet_for_client,
        exchange_address=EXCHANGE_ADDRESS_STANDARD,
        funder=funder,
        eoa_address=eoa_address,
        signature_type_int=signature_type_int
    )
    
    # Store private key
    final_client._private_key = private_key
    
    return final_client


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

