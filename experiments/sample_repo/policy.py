MAX_DELAY_SECONDS = 32
MAX_ATTEMPTS = 8


def delay(attempt):
    """Increase the wait between failed network requests, capped to avoid long stalls."""
    return min(2 ** attempt, MAX_DELAY_SECONDS)
