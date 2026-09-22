"""The machine shape registry the publisher prices: catalog/gcp-machine-shapes.json."""

from decimal import Decimal
from pathlib import Path

import pytest
from gcp_pricing.shapes import ShapeRegistryError, load_shapes

REGISTRY = Path("catalog/gcp-machine-shapes.json")


def test_the_registry_has_exactly_the_37_verified_shapes() -> None:
    shapes = load_shapes(REGISTRY)
    assert len(shapes) == 37
    assert len({shape.machine_type for shape in shapes}) == 37


def test_highcpu_memory_is_the_official_1_gib_per_vcpu_not_the_runner_heuristic() -> None:
    """The GCP Batch runner's 0.9 GB/vCPU is a selection heuristic, not the
    machine's billable RAM; the catalog must price the real 1 GiB/vCPU.
    """
    shapes = {shape.machine_type: shape for shape in load_shapes(REGISTRY)}
    assert shapes["n2-highcpu-8"].memory_gib == Decimal("8")


def test_n2_highcpu_128_is_absent_from_the_registry() -> None:
    """The runner's common-size list can construct n2-highcpu-128, which is not
    in the official N2 highcpu table; the publisher must not invent a rate for it.
    """
    machine_types = {shape.machine_type for shape in load_shapes(REGISTRY)}
    assert "n2-highcpu-128" not in machine_types
    assert "n2-highcpu-96" in machine_types


def test_family_and_variant_vcpu_sizes_match_the_published_specification() -> None:
    shapes = load_shapes(REGISTRY)
    by_variant: dict[tuple[str, str], list[int]] = {}
    for shape in shapes:
        by_variant.setdefault((shape.family, shape.variant), []).append(shape.vcpu)
    common = [1, 2, 4, 8, 16, 32, 48, 60]
    n2_sizes = [2, 4, 8, 16, 32, 48, 64, 80, 96, 128]
    assert sorted(by_variant[("t2d", "standard")]) == common
    assert sorted(by_variant[("n2", "standard")]) == n2_sizes
    assert sorted(by_variant[("n2", "highmem")]) == n2_sizes
    assert sorted(by_variant[("n2", "highcpu")]) == [v for v in n2_sizes if v != 128]


def test_memory_per_vcpu_ratio_holds_for_every_shape() -> None:
    ratios = {"standard": Decimal("4"), "highmem": Decimal("8"), "highcpu": Decimal("1")}
    for shape in load_shapes(REGISTRY):
        assert shape.memory_gib == shape.vcpu * ratios[shape.variant]


def test_a_duplicate_machine_type_is_rejected(tmp_path) -> None:
    import json

    bad = tmp_path / "shapes.json"
    bad.write_text(
        json.dumps(
            {
                "shapes": [
                    {
                        "machine_type": "n2-standard-2",
                        "family": "n2",
                        "variant": "standard",
                        "vcpu": 2,
                        "memory_gib": "8",
                        "source_ref": "https://example.invalid",
                    }
                ]
                * 2
            }
        )
    )
    with pytest.raises(ShapeRegistryError, match="duplicate"):
        load_shapes(bad)


def test_a_missing_registry_file_is_rejected(tmp_path) -> None:
    with pytest.raises(ShapeRegistryError, match="not found"):
        load_shapes(tmp_path / "missing.json")
