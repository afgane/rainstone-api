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
    boot.add_argument(
        "--replace-source",
        action="store_true",
        help="Enroll a different source database for this instance, discarding the old binding",
    )
    boot.add_argument(
        "--write-secret",
        default=None,
        help="Kubernetes Secret to receive the scoped reader DSN, as name[/key]",
    )

    ready = commands.add_parser(
        "wait-ready", help="Block until migrations and instance enrollment are complete"
    )
    ready.add_argument("--timeout", type=int, default=600)
    ready.add_argument("--interval", type=int, default=5)

    heartbeat = commands.add_parser(
        "heartbeat", help="Check that the collector loop wrote a recent heartbeat"
    )
    heartbeat.add_argument("--max-age", type=int, default=300)

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
                replace_source=args.replace_source,
                secret_target=args.write_secret,
            )
        )
        return

    if args.command == "wait-ready":
        import time

        from rainstone.doctor import readiness

        deadline = time.monotonic() + args.timeout
        while True:
            with Session(engine) as session:
                state = readiness(session)
            if state["ready"]:
                _print({"ready": True, **state["facts"]})
                return
            if time.monotonic() >= deadline:
                _print({"ready": False, "reasons": state["reasons"]})
                sys.exit(1)
            print(
                json.dumps({"waiting_for": state["reasons"]}, sort_keys=True),
                file=sys.stderr,
                flush=True,
            )
            time.sleep(args.interval)

    if args.command == "heartbeat":
        import time

        from rainstone.config import get_settings

        path = get_settings().collector_heartbeat_path
        if not path.exists():
            _print({"alive": False, "reason": "no heartbeat has been written yet"})
            sys.exit(1)
        age = time.time() - path.stat().st_mtime
        alive = age <= args.max_age
        _print({"alive": alive, "age_seconds": round(age, 1)})
        sys.exit(0 if alive else 1)

    if args.command == "doctor":
        from rainstone.config import get_settings as _settings
        from rainstone.doctor import record_report, run_checks
        from rainstone.ingestion import stable_id

        settings = _settings()
        with Session(engine) as session:
            report = run_checks(session, settings, context="collector")
            try:
                record_report(session, stable_id("tenant", settings.tenant_slug), report)
                session.commit()
            except Exception:  # noqa: BLE001 - reporting must not mask findings
                session.rollback()
        _print(report)
        sys.exit(1 if report["overall_status"] == "fail" else 0)

    if args.command in {"catalog", "validate-catalog"}:
        from rainstone.catalog import (
            CatalogError,
            coverage,
            load_file,
            parse_trusted_keys,
            refresh,
        )
        from rainstone.catalog import import_catalog as import_artifact
        from rainstone.config import get_settings

        settings = get_settings()
        action = "validate" if args.command == "validate-catalog" else args.catalog_command
        try:
            trusted = parse_trusted_keys(settings.catalog_trusted_keys)
            if action == "validate":
                catalog = load_file(
                    args.path,
                    require_signature=settings.catalog_require_signature,
                    trusted_keys=trusted,
                )
                _print(
                    {
                        "catalog_id": catalog.catalog_id,
                        "rates": len(catalog.rates),
                        "digest": catalog.digest,
                        "signature_verified": catalog.signature_verified,
                        "signature_key_id": catalog.signature_key_id,
                        "valid": True,
                    }
                )
                return
            if action == "import":
                catalog = load_file(
                    args.path,
                    require_signature=settings.catalog_require_signature,
                    trusted_keys=trusted,
                )
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
                            trusted_keys=trusted,
                        )
                    )
                return
            with Session(engine) as session:
                _print(coverage(session))
        except CatalogError as error:
            parser.error(str(error))


if __name__ == "__main__":
    main()
