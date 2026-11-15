"""Round final non-negative display values; keep scoring inputs at full precision."""
from decimal import Decimal, ROUND_HALF_UP


def round_final(value: float, decimal_places: int = 0):
    rounded = Decimal(str(value)).quantize(
        Decimal(1).scaleb(-decimal_places), rounding=ROUND_HALF_UP,
    )
    return int(rounded) if decimal_places == 0 else float(rounded)
