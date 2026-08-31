"""Fixed-point arithmetic utilities for deterministic financial calculations."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Self
else:
    Self = "FixedPoint"

__all__ = [
    "FixedPoint",
    "FP_ZERO",
    "FP_ONE",
    "scale_from_decimal",
    "scale_to_decimal",
]

# Scale factor: 10^8 (8 decimal places) - standard for crypto/fiat precision
SCALE = 10**8
SCALE_DECIMAL = Decimal(SCALE)


class FixedPoint:
    """
    Fixed-point integer representation for financial values.

    Internally stores value as integer scaled by 10^8.
    All arithmetic operations maintain precision without floating-point errors.
    """

    __slots__ = ("_value",)

    def __init__(self, value: int | float | Decimal | str | Self, *, _raw: bool = False) -> None:
        if isinstance(value, FixedPoint):
            self._value = value._value
        elif isinstance(value, int):
            if _raw:
                self._value = value
            else:
                # Scale integer by 10^8
                self._value = value * SCALE
        elif isinstance(value, float):
            # Convert float via Decimal to avoid binary floating-point artifacts
            self._value = int(Decimal(str(value)) * SCALE_DECIMAL)
        elif isinstance(value, Decimal):
            self._value = int(value * SCALE_DECIMAL)
        elif isinstance(value, str):
            self._value = int(Decimal(value) * SCALE_DECIMAL)
        else:
            raise TypeError(f"Cannot create FixedPoint from {type(value)}")

    @classmethod
    def _from_raw(cls, value: int) -> Self:
        """Create FixedPoint from already-scaled integer value (internal use)."""
        return cls(value, _raw=True)

    @property
    def value(self) -> int:
        """Raw integer value (scaled by 10^8)."""
        return self._value

    def __int__(self) -> int:
        return self._value

    def __float__(self) -> float:
        return float(self._value) / SCALE

    def __str__(self) -> str:
        return self.to_decimal().__str__()

    def __repr__(self) -> str:
        return f"FixedPoint({self.to_decimal()})"

    def to_decimal(self) -> Decimal:
        """Convert to Decimal for display."""
        return Decimal(self._value) / SCALE_DECIMAL

    # Comparison
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FixedPoint):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __lt__(self, other: Self) -> bool:
        if not isinstance(other, FixedPoint):
            return NotImplemented
        return self._value < other._value

    def __le__(self, other: Self) -> bool:
        if not isinstance(other, FixedPoint):
            return NotImplemented
        return self._value <= other._value

    def __gt__(self, other: Self) -> bool:
        if not isinstance(other, FixedPoint):
            return NotImplemented
        return self._value > other._value

    def __ge__(self, other: Self) -> bool:
        if not isinstance(other, FixedPoint):
            return NotImplemented
        return self._value >= other._value

    # Arithmetic
    def __add__(self, other: Self | int | Decimal) -> Self:
        if isinstance(other, FixedPoint):
            return FixedPoint._from_raw(self._value + other._value)
        if isinstance(other, int):
            return FixedPoint._from_raw(self._value + other * SCALE)
        if isinstance(other, Decimal):
            return FixedPoint._from_raw(self._value + int(other * SCALE_DECIMAL))
        return NotImplemented

    def __sub__(self, other: Self | int | Decimal) -> Self:
        if isinstance(other, FixedPoint):
            return FixedPoint._from_raw(self._value - other._value)
        if isinstance(other, int):
            return FixedPoint._from_raw(self._value - other * SCALE)
        if isinstance(other, Decimal):
            return FixedPoint._from_raw(self._value - int(other * SCALE_DECIMAL))
        return NotImplemented

    def __mul__(self, other: Self | int | Decimal) -> Self:
        if isinstance(other, FixedPoint):
            # (a * b) / SCALE to maintain scale
            return FixedPoint._from_raw((self._value * other._value) // SCALE)
        if isinstance(other, int):
            return FixedPoint._from_raw(self._value * other)
        if isinstance(other, Decimal):
            return FixedPoint._from_raw(int(self._value * other))
        return NotImplemented

    def __truediv__(self, other: Self | int | Decimal) -> Self:
        if isinstance(other, FixedPoint):
            if other._value == 0:
                raise ZeroDivisionError("Division by zero")
            # (a * SCALE) / b to maintain scale
            return FixedPoint._from_raw((self._value * SCALE) // other._value)
        if isinstance(other, int):
            if other == 0:
                raise ZeroDivisionError("Division by zero")
            return FixedPoint._from_raw(self._value // other)
        if isinstance(other, Decimal):
            if other == 0:
                raise ZeroDivisionError("Division by zero")
            return FixedPoint._from_raw(int(self._value / other))
        return NotImplemented

    def __floordiv__(self, other: Self | int) -> Self:
        if isinstance(other, FixedPoint):
            if other._value == 0:
                raise ZeroDivisionError("Division by zero")
            return FixedPoint._from_raw((self._value * SCALE) // other._value)
        if isinstance(other, int):
            if other == 0:
                raise ZeroDivisionError("Division by zero")
            return FixedPoint._from_raw(self._value // other)
        return NotImplemented

    def __mod__(self, other: Self | int) -> Self:
        if isinstance(other, FixedPoint):
            return FixedPoint._from_raw(self._value % other._value)
        if isinstance(other, int):
            return FixedPoint._from_raw(self._value % (other * SCALE))
        return NotImplemented

    def __neg__(self) -> Self:
        return FixedPoint._from_raw(-self._value)

    def __abs__(self) -> Self:
        return FixedPoint._from_raw(abs(self._value))

    def __radd__(self, other: int | Decimal) -> Self:
        return self.__add__(other)

    def __rsub__(self, other: int | Decimal) -> Self:
        if isinstance(other, int):
            return FixedPoint._from_raw(other * SCALE - self._value)
        if isinstance(other, Decimal):
            return FixedPoint._from_raw(int(other * SCALE_DECIMAL) - self._value)
        return NotImplemented

    def __rmul__(self, other: int | Decimal) -> Self:
        return self.__mul__(other)

    def __rtruediv__(self, other: int | Decimal) -> Self:
        if isinstance(other, int):
            return FixedPoint._from_raw((other * SCALE) // self._value)
        if isinstance(other, Decimal):
            return FixedPoint._from_raw(int(other * SCALE_DECIMAL / self.to_decimal()))
        return NotImplemented

    # Utility methods
    def is_zero(self) -> bool:
        return self._value == 0

    def is_positive(self) -> bool:
        return self._value > 0

    def is_negative(self) -> bool:
        return self._value < 0

    def abs(self) -> Self:
        return FixedPoint(abs(self._value))

    def min(self, other: Self) -> Self:
        return self if self < other else other

    def max(self, other: Self) -> Self:
        return self if self > other else other

    def clamp(self, min_val: Self, max_val: Self) -> Self:
        if self < min_val:
            return min_val
        if self > max_val:
            return max_val
        return self


# Constants
FP_ZERO = FixedPoint(0)
FP_ONE = FixedPoint("1")


def scale_from_decimal(d: Decimal) -> int:
    """Convert Decimal to scaled integer."""
    return int(d * SCALE_DECIMAL)


def scale_to_decimal(i: int) -> Decimal:
    """Convert scaled integer to Decimal."""
    return Decimal(i) / SCALE_DECIMAL
