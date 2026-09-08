from policy import MAX_ATTEMPTS, delay


def wait_before_retry(attempt):
    if attempt >= MAX_ATTEMPTS:
        raise ValueError("retry budget exhausted")
    return delay(attempt)


def color_hex(red, green, blue):
    """Convert RGB channels to a display color."""
    return f"#{red:02x}{green:02x}{blue:02x}"
