from __future__ import annotations

import argparse
import sys
from pathlib import Path

from granola_export import __version__
from granola_export.auth import load_token, receive_desktop_token
from granola_export.client import GranolaClient
from granola_export.errors import ExportError
from granola_export.exporter import Exporter, ExportOptions


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not 0 <= number < float("inf"):
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export all accessible Granola meetings and notes."
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Export own and shared notes with transcripts")
    export.add_argument("--output", "-o", type=Path, default=Path("exports/granola"))
    auth = export.add_mutually_exclusive_group()
    auth.add_argument(
        "--auth-file", type=Path, help="Plain JSON session file containing WorkOS tokens"
    )
    auth.add_argument(
        "--desktop-auth", action="store_true", help="Connect via Granola's developer console"
    )
    export.add_argument("--token-env", default="GRANOLA_ACCESS_TOKEN")
    export.add_argument(
        "--resume", action="store_true", help="Reuse successful nonempty unchanged payloads"
    )
    export.add_argument("--no-auxiliary", action="store_true")
    export.add_argument("--no-attachments", action="store_true")
    export.add_argument(
        "--document-id", action="append", default=[], help="Also fetch this note ID"
    )
    export.add_argument("--page-size", type=positive_int, default=100)
    export.add_argument(
        "--delay", type=nonnegative_float, default=0.2, help="Seconds between API requests"
    )
    export.add_argument("--timeout", type=positive_int, default=30)
    args = parser.parse_args(argv)
    if args.page_size > 100:
        parser.error("--page-size must be at most 100")
    try:
        token = (
            receive_desktop_token(print)
            if args.desktop_auth
            else load_token(args.auth_file, args.token_env)
        )
        options = ExportOptions(
            output=args.output,
            page_size=args.page_size,
            resume=args.resume,
            auxiliary=not args.no_auxiliary,
            attachments=not args.no_attachments,
            document_ids=tuple(args.document_id),
        )
        with GranolaClient(token, timeout=args.timeout, delay=args.delay) as client:
            result = Exporter(client, options).run()
        print(
            f"Export {result['status']}: {result['exported_count']}/{result['document_count']} "
            f"notes in {args.output.expanduser().resolve()}"
        )
        print(
            "Transcripts: "
            + ", ".join(
                f"{count} {status}" for status, count in result["summary"]["transcript"].items()
            )
        )
        print("See manifest.json for coverage, empty transcripts, and unavailable resources.")
        return 0 if result["status"] == "complete" else 2
    except ExportError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Filesystem error: {error.strerror}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Export interrupted; use --resume to continue.", file=sys.stderr)
        return 130
