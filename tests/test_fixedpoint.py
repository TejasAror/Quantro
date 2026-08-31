"""Tests for fixed-point arithmetic."""

from decimal import Decimal

import pytest

from quantro.fixedpoint import FP_ONE, FP_ZERO, FixedPoint, scale_from_decimal, scale_to_decimal


class TestFixedPointCreation:
    """Test FixedPoint creation from various types."""

    def test_from_int(self):
        fp = FixedPoint(42)
        assert fp.value == 42 * 10**8

    def test_from_float(self):
        fp = FixedPoint(3.14)
        assert fp.value == 314000000

    def test_from_decimal(self):
        fp = FixedPoint(Decimal("2.71828"))
        assert fp.value == 271828000

    def test_from_string(self):
        fp = FixedPoint("1.5")
        assert fp.value == 150000000

    def test_from_fixedpoint(self):
        fp1 = FixedPoint("1.5")
        fp2 = FixedPoint(fp1)
        assert fp1 == fp2


class TestFixedPointComparison:
    """Test comparison operations."""

    def test_eq(self):
        assert FixedPoint("1.0") == FixedPoint("1.0")
        assert not (FixedPoint("1.0") == FixedPoint("2.0"))

    def test_lt(self):
        assert FixedPoint("1.0") < FixedPoint("2.0")
        assert not (FixedPoint("2.0") < FixedPoint("1.0"))

    def test_le(self):
        assert FixedPoint("1.0") <= FixedPoint("1.0")
        assert FixedPoint("1.0") <= FixedPoint("2.0")

    def test_gt(self):
        assert FixedPoint("2.0") > FixedPoint("1.0")

    def test_ge(self):
        assert FixedPoint("2.0") >= FixedPoint("1.0")
        assert FixedPoint("1.0") >= FixedPoint("1.0")


class TestFixedPointArithmetic:
    """Test arithmetic operations."""

    def test_add(self):
        a = FixedPoint("1.5")
        b = FixedPoint("2.5")
        result = a + b
        assert result == FixedPoint("4.0")

    def test_add_int(self):
        a = FixedPoint("1.5")
        result = a + 2
        assert result == FixedPoint("3.5")

    def test_sub(self):
        a = FixedPoint("5.0")
        b = FixedPoint("2.0")
        result = a - b
        assert result == FixedPoint("3.0")

    def test_mul(self):
        a = FixedPoint("2.0")
        b = FixedPoint("3.0")
        result = a * b
        assert result == FixedPoint("6.0")

    def test_mul_int(self):
        a = FixedPoint("2.5")
        result = a * 4
        assert result == FixedPoint("10.0")

    def test_div(self):
        a = FixedPoint("10.0")
        b = FixedPoint("2.0")
        result = a / b
        assert result == FixedPoint("5.0")

    def test_div_int(self):
        a = FixedPoint("10.0")
        result = a / 2
        assert result == FixedPoint("5.0")

    def test_neg(self):
        a = FixedPoint("5.0")
        result = -a
        assert result == FixedPoint("-5.0")

    def test_abs(self):
        a = FixedPoint("-5.0")
        result = abs(a)
        assert result == FixedPoint("5.0")

    def test_zero_division(self):
        a = FixedPoint("1.0")
        with pytest.raises(ZeroDivisionError):
            a / FixedPoint(0)

    def test_min_max(self):
        a = FixedPoint("1.0")
        b = FixedPoint("2.0")
        assert a.min(b) == FixedPoint("1.0")
        assert a.max(b) == FixedPoint("2.0")

    def test_clamp(self):
        a = FixedPoint("5.0")
        result = a.clamp(FixedPoint("1.0"), FixedPoint("3.0"))
        assert result == FixedPoint("3.0")

        result = a.clamp(FixedPoint("6.0"), FixedPoint("10.0"))
        assert result == FixedPoint("6.0")

        result = a.clamp(FixedPoint("1.0"), FixedPoint("10.0"))
        assert result == FixedPoint("5.0")


class TestFixedPointProperties:
    """Test property methods."""

    def test_is_zero(self):
        assert FixedPoint(0).is_zero()
        assert not FixedPoint("0.00000001").is_zero()

    def test_is_positive(self):
        assert FixedPoint("1.0").is_positive()
        assert not FixedPoint("-1.0").is_positive()
        assert not FixedPoint(0).is_positive()

    def test_is_negative(self):
        assert FixedPoint("-1.0").is_negative()
        assert not FixedPoint("1.0").is_negative()
        assert not FixedPoint(0).is_negative()

    def test_to_decimal(self):
        fp = FixedPoint("1.23456789")
        d = fp.to_decimal()
        assert d == Decimal("1.23456789")

    def test_to_float(self):
        fp = FixedPoint("1.5")
        assert float(fp) == 1.5

    def test_to_int(self):
        fp = FixedPoint("1.5")
        # int() returns raw scaled value
        assert int(fp) == 150000000


class TestConstants:
    """Test constant values."""

    def test_fp_zero(self):
        assert FP_ZERO.value == 0
        assert FP_ZERO == FixedPoint(0)

    def test_fp_one(self):
        assert FP_ONE.value == 10**8
        assert FP_ONE == FixedPoint(1)


class TestScaleFunctions:
    """Test scale conversion functions."""

    def test_scale_from_decimal(self):
        d = Decimal("1.5")
        scaled = scale_from_decimal(d)
        assert scaled == 150000000

    def test_scale_to_decimal(self):
        scaled = 150000000
        d = scale_to_decimal(scaled)
        assert d == Decimal("1.5")


class TestPrecision:
    """Test precision handling."""

    def test_high_precision(self):
        a = FixedPoint("0.00000001")
        b = FixedPoint("0.00000002")
        result = a + b
        assert result == FixedPoint("0.00000003")

    def test_large_numbers(self):
        a = FixedPoint("1000000.0")
        b = FixedPoint("2000000.0")
        result = a + b
        assert result == FixedPoint("3000000.0")

    def test_multiplication_precision(self):
        # 1.5 * 2.0 = 3.0 exactly
        a = FixedPoint("1.5")
        b = FixedPoint("2.0")
        result = a * b
        assert result == FixedPoint("3.0")

    def test_division_precision(self):
        # 10 / 3 = 3.33333333 (truncated)
        a = FixedPoint("10.0")
        b = FixedPoint("3.0")
        result = a / b
        assert result == FixedPoint("3.33333333")
