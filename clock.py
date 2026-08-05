import urllib.request
import urllib.error
import json

API = "https://blockstream.info/api"


def _fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode().strip()
    except (urllib.error.URLError, OSError):
        return None


def get_tip():
    height_str = _fetch(f"{API}/blocks/tip/height")
    if height_str is None:
        return None, None
    try:
        height = int(height_str)
    except ValueError:
        return None, None
    block_hash = _fetch(f"{API}/block-height/{height}")
    return height, block_hash


class BitcoinClock:
    def __init__(self):
        self.last_height = None
        self.last_hash = None

    def poll(self):
        """Return new block dict if a new block has been seen, else None."""
        height, block_hash = get_tip()
        if height is None:
            return None
        if height != self.last_height:
            self.last_height = height
            self.last_hash = block_hash
            return {'height': height, 'hash': block_hash}
        return None
