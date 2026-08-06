import urllib.request
import urllib.error

API_MAIN = "https://blockstream.info/api"
API_TEST = "https://blockstream.info/testnet/api"


def _fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode().strip()
    except (urllib.error.URLError, OSError):
        return None


def get_tip(testnet: bool = False):
    api = API_TEST if testnet else API_MAIN
    height_str = _fetch(f"{api}/blocks/tip/height")
    if height_str is None:
        return None, None
    try:
        height = int(height_str)
    except ValueError:
        return None, None
    block_hash = _fetch(f"{api}/block-height/{height}")
    return height, block_hash


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
