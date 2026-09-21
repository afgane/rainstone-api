import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from rainstone.db import engine
from rainstone.ingestion import ingest_fixture


def _print(payload: dict | list) -> None:
    print(json.dumps(payload, sort_keys=True, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(prog="rainstone")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser(
        "ingest-fixtures", help="Idempotently ingest normalized fixture observations"
    )
    ingest.add_argument("--path", type=Path, required=True)

    collect = commands.add_parser("collect", help="Run the unattended collector")
    collect.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="Run this many cycles and exit; omit to run until stopped",
    )

    boot = commands.add_parser(
        "bootstrap", help="Provision the scoped Galaxy reader and seed instance identity"
    )
    boot.add_argument(
        "--admin-database-url",
        required=True,
        help="Installation-time Galaxy DSN; the application never receives it",
    )
    boot.add_argument("--shared-account", required=True, help="Galaxy account to report on")
    boot.add_argument("--reader-role", default="rainstone_reader")
    boot.add_argument("--rotate", action="store_true", help="Rotate the reader credential")
    boot.add_argument("--write-dsn", type=Path, default=None, help="Write the scoped reader DSN here")
    boot.add_argument("--descriptor", type=Path, default=None, help="JSON deployment descriptor")

    doctor = commands.add_parser("doctor", help="Run read-only self-checks")
    doctor.add_argument("--json", action="store_true", help="Emit JSON (default)")

    catalog = commands.add_parser("catalog", help="Manage the price catalog")
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    validate = catalog_commands.add_parser("validate", help="Validate a catalog artifact")
    validate.add_argument("--path", type=Path, required=True)
    import_catalog = catalog_commands.add_parser("import", help="Import a catalog artifact")
    import_catalog.add_argument("--path", type=Path, required=True)
    catalog_commands.add_parser("refresh", help="Refresh from the configured feed")
    catalog_commands.add_parser("coverage", help="Show priced shape and region coverage")

    # Retained for compatibility with the Phase 2A command name.
    legacy = commands.add_parser("validate-catalog", help="Validate a bundled price catalog")
    legacy.add_argument("--path", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "ingest-fixtures":
        with Session(engine) as session:
            _print(ingest_fixture(session, args.path))
        return

    if args.command == "collect":
        from rainstone.collector import CollectorLeaseUnavailable, run

        try:
            results = run(cycles=args.cycles)
        except CollectorLeaseUnavailable as error:
            _print({"status": "lease_unavailable", "detail": str(error)})
            sys.exit(75)
        if args.cycles is not None:
            _print(results)
        return

    if args.command == "bootstrap":
        from rainstone.bootstrap import bootstrap

        descriptor = json.loads(args.descriptor.read_text()) if args.descriptor else {}
        _print(
            bootstrap(
                admin_database_url=args.admin_database_url,
                shared_account=args.shared_account,
                reader_role=args.reader_role,
                rotate=args.rotate,
                dsn_output=args.write_dsn,
                descriptor=descriptor,
            )
        )
        return

    if args.command == "doctor":
        from rainstone.doctor import run_checks

        with Session(engine) as session:
            report = run_checks(session)
        _print(report)
        sys.exit(1 if report["overall_status"] == "fail" else 0)

    if args.command in {"catalog", "validate-catalog"}:
        from rainstone.catalog import CatalogError, coverage, load_file, refresh
        from rainstone.catalog import import_catalog as import_artifact
        from rainstone.config import get_settings

        settings = get_settings()
        action = "validate" if args.command == "validate-catalog" else args.catalog_command
        try:
            if action == "validate":
                catalog = load_file(args.path, require_signature=settings.catalog_require_signature)
                _print(
                    {
                        "catalog_id": catalog.catalog_id,
                        "rates": len(catalog.rates),
                        "digest": catalog.digest,
                        "valid": True,
                    }
                )
                return
            if action == "import":
                catalog = load_file(args.path, require_signature=settings.catalog_require_signature)
                with Session(engine) as session:
                    result = import_artifact(session, catalog)
                    session.commit()
                _print(result)
                return
            if action == "refresh":
                with Session(engine) as session:
                    _print(
                        refresh(
                            session,
                            url=settings.catalog_feed_url,
                            bundled_path=settings.catalog_path,
                            require_signature=settings.catalog_require_signature,
                        )
                    )
                return
            with Session(engine) as session:
                _print(coverage(session))
        except CatalogError as error:
            parser.error(str(error))


if __name__ == "__main__":
    main()
