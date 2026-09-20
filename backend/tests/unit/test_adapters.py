from decimal import Decimal

from rainstone.adapters.kubernetes import parse_cpu, parse_memory_mib


def test_kubernetes_quantities_preserve_units() -> None:
    assert parse_cpu("500m") == Decimal("0.5")
    assert parse_memory_mib("3.8Gi") == Decimal("3891.2")
    assert parse_memory_mib("4080218931200m") == Decimal("3891.2")
