import time
import urllib.request
import urllib.error

# Two independent block explorers, tried in order, each retried a couple
# times before moving on -- a single API having a transient blip (or a
# temporary outage) used to have zero retry at all: one failed request
# and get_tip() gave up entirely, returning (None, None). ott.py's
# cmd_commit() treats that as "network unreachable" and silently falls
# back to committing with a synthetic, non-Bitcoin block_height=0 --
# permanently, since the commitment hash is derived from whatever block
# hash was used at commit time and can't be retroactively fixed once
# written to the ledger. Real incident: exactly this happened from one
# ordinary transient blip against blockstream.info, and verify_chain only
# caught it much later, as an unverifiable ledger entry with no way to
# repair it after the fact -- only to commit again and get a real one.
# Retrying, and falling through to a second independent host, actually
# covers "one service hiccuped for a moment," the failure mode that
# happened, instead of only ever covering "the whole internet is down."
APIS = [
    {"main": "https://blockstream.info/api", "test": "https://blockstream.info/testnet/api"},
    {"main": "https://mempool.space/api", "test": "https://mempool.space/testnet/api"},
]

# kept for anyone importing these directly (previously the only way to
# know which host get_tip() talks to) -- still just blockstream.info,
# the first entry in APIS above
API_MAIN = APIS[0]["main"]
API_TEST = APIS[0]["test"]


def _fetch(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode().strip()
    except (urllib.error.URLError, OSError):
        return None


def _get_tip_from(api, retries=2, backoff=1.5):
    for attempt in range(retries + 1):
        height_str = _fetch(f"{api}/blocks/tip/height")
        if height_str is not None:
            try:
                height = int(height_str)
            except ValueError:
                return None, None
            block_hash = _fetch(f"{api}/block-height/{height}")
            if block_hash is not None:
                return height, block_hash
        if attempt < retries:
            time.sleep(backoff)
    return None, None


def get_tip(testnet: bool = False):
    key = "test" if testnet else "main"
    for api_pair in APIS:
        height, block_hash = _get_tip_from(api_pair[key])
        if height is not None and block_hash is not None:
            return height, block_hash
    return None, None


class BitcoinClock:
    def __init__(self, testnet: bool = False):
        self.testnet = testnet
        self.last_height = None
        self.last_hash = None

    def poll(self):
        """Return new block dict if a new block has been seen, else None."""
        height, block_hash = get_tip(self.testnet)
        if height is None:
            return None
        if height != self.last_height:
            self.last_height = height
            self.last_hash = block_hash
            return {'height': height, 'hash': block_hash}
        return None
