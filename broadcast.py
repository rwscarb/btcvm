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
    key.get_unspents()  # force fresh UTXO fetch including mempool
    # Pass hex string directly; bit encodes as UTF-8, producing 64 ASCII bytes in OP_RETURN.
    # fee=2 sat/vbyte — low priority, safe for non-urgent OP_RETURN commitments.
    # Overrides the fee API default (which spikes to 72+ when the API is unreachable).
    tx_hash = key.send([], message=commitment_hex[:64], fee=2)
    return tx_hash


def bump_fee(wif: str, fee: int, network: str = 'testnet') -> str:
    """
    CPFP fee bump for a stuck broadcast: spends this wallet's own pending
    (even unconfirmed) change output in a brand-new transaction at a
    higher fee rate. Since the new tx spends an output from the stuck
    parent, miners can only collect the higher combined fee by confirming
    both together — that's what pulls the stuck tx along with it.

    Not BIP125 RBF (replacing the stuck tx outright) — `bit`'s send()
    doesn't expose sequence-number control for that. CPFP doesn't need
    the original tx to have signaled anything though, just a spendable
    output still sitting in this same wallet, so it works regardless.

    Returns the new transaction hash.
    """
    check_available()
    key = load_key(wif, network)
    key.get_unspents()  # includes mempool — this is what makes CPFP possible
    if not key.unspents:
        raise RuntimeError(
            'No spendable UTXOs (confirmed or unconfirmed) for this wallet — '
            'nothing to bump (or the stuck tx already confirmed)'
        )
    return key.send([], fee=fee)


def get_balance(wif: str, network: str = 'testnet') -> int:
    """Return balance in satoshis."""
    check_available()
    key = load_key(wif, network)
    # bit's get_balance() returns a string despite the amount always being
    # a whole number of satoshis — cast so this function's own -> int
    # contract is actually true, not just a type hint that happens to lie.
    return int(key.get_balance('satoshi'))


def generate_key(network: str = 'testnet'):
    """Generate a fresh wallet key for broadcasting commitments. Returns the
    bit key object — caller reads .to_wif() and .address off it."""
    check_available()
    return PrivateKey() if network == 'mainnet' else PrivateKeyTestnet()
