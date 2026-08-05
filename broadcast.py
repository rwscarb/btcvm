"""
Optional OP_RETURN broadcast for BTC-Clocked VM.
Requires: pip install bit
Uses Bitcoin testnet by default. Pass network='mainnet' for real BTC.
"""

try:
    from bit import PrivateKeyTestnet, PrivateKey
    BIT_AVAILABLE = True
except ImportError:
    BIT_AVAILABLE = False


def check_available():
    if not BIT_AVAILABLE:
        raise RuntimeError(
            "The 'bit' library is required for broadcast.\n"
            "Install it with: pip install bit"
        )


def load_key(wif: str, network: str = 'testnet'):
    check_available()
    if network == 'mainnet':
        return PrivateKey(wif)
    return PrivateKeyTestnet(wif)


def broadcast_commitment(commitment_hex: str, wif: str, network: str = 'testnet') -> str:
    """
    Broadcast an OP_RETURN transaction embedding commitment_hex (64 hex chars = 32 bytes).
    Returns the transaction hash.
    """
    check_available()
    key = load_key(wif, network)
    # Pass hex string directly; bit encodes as UTF-8, producing 64 ASCII bytes in OP_RETURN.
    tx_hash = key.send([], message=commitment_hex[:64])
    return tx_hash


def get_balance(wif: str, network: str = 'testnet') -> int:
    """Return balance in satoshis."""
    check_available()
    key = load_key(wif, network)
    return key.get_balance('satoshi')
