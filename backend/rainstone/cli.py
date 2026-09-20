import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from rainstone.db import engine
from rainstone.ingestion import ingest_fixture


def main() -> None:
    parser = argparse.ArgumentParser(prog="rainstone")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser(
        "ingest-fixtures", help="Idempotently ingest normalized fixture observations"
    )
    ingest.add_argument("--path", type=Path, required=True)
    validate = commands.add_parser("validate-catalog", help="Validate a bundled price catalog")
    validate.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "ingest-fixtures":
        with Session(engine) as session:
            print(json.dumps(ingest_fixture(session, args.path), sort_keys=True))
    elif args.command == "validate-catalog":
        payload = json.loads(args.path.read_text())
        required = {"schema_version", "catalog_id", "observed_at", "currency", "rates", "source_urls"}
        missing = required - payload.keys()
        if missing:
            parser.error(f"catalog is missing fields: {', '.join(sorted(missing))}")
        if len(
            {(r["provider"], r["region"], r["purchase_model"], r["machine_type"]) for r in payload["rates"]}
        ) != len(payload["rates"]):
            parser.error("catalog contains duplicate resolver keys")
        print(
            json.dumps({"catalog_id": payload["catalog_id"], "rates": len(payload["rates"]), "valid": True})
        )


if __name__ == "__main__":
    main()
