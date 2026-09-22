"""The checked-in registry of machine shapes this publisher prices.

Regional prices are never stored here: the registry names which shapes exist
and how large they are, and the Catalog API supplies the money.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

DEFAULT_PATH = Path("catalog/gcp-machine-shapes.json")


class ShapeRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class MachineShape:
    machine_type: str
    family: str  # "t2d" or "n2"
    variant: str  # "standard", "highmem" or "highcpu"
    vcpu: int
    memory_gib: Decimal
    source_ref: str


def load_shapes(path: Path = DEFAULT_PATH) -> tuple[MachineShape, ...]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise ShapeRegistryError(f"machine shape registry not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ShapeRegistryError(f"machine shape registry is not valid JSON: {error}") from error
    shapes: list[MachineShape] = []
    seen: set[str] = set()
    for entry in data.get("shapes", []):
        missing = {"machine_type", "family", "variant", "vcpu", "memory_gib", "source_ref"} - entry.keys()
        if missing:
            raise ShapeRegistryError(f"shape entry is missing fields: {', '.join(sorted(missing))}")
        if entry["machine_type"] in seen:
            raise ShapeRegistryError(f"duplicate machine shape: {entry['machine_type']}")
        seen.add(entry["machine_type"])
        shapes.append(
            MachineShape(
                machine_type=entry["machine_type"],
                family=entry["family"],
                variant=entry["variant"],
                vcpu=int(entry["vcpu"]),
                memory_gib=Decimal(str(entry["memory_gib"])),
                source_ref=entry["source_ref"],
            )
        )
    if not shapes:
        raise ShapeRegistryError(f"machine shape registry at {path} has no shapes")
    return tuple(shapes)
