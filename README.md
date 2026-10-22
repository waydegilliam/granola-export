# granola-export

A Python package and CLI that exports accessible Granola meetings, shared notes,
AI summaries, transcripts, inline images, and auxiliary account data to local JSON
and Markdown. Inspired by [moona3k/granola-export](https://github.com/moona3k/granola-export).

Requires Python 3.11 or newer.

```sh
uv sync
uv run granola-export export --desktop-auth
```

The command prints a one-time JavaScript command. In Granola, open **View → Toggle
Developer Tools → Console** and run that command. Some desktop versions require
selecting Toggle Developer Tools five times before it opens. Review any console
paste warning yourself. The command obtains a refreshed session from the app and
sends it to this process at a random, single-use loopback address. It never prints
or persists the token. Keep Granola signed in during the export.

The default output is `exports/granola/`, excluded from Git. To choose a destination:

```sh
uv run granola-export export --desktop-auth --output ~/Backups/granola
```

You can also install the CLI with `uv tool install .`, then run
`granola-export export`. `python -m granola_export` is equivalent.

## What is exported

- Own meetings and standalone notes, without date or meeting-type filters.
- Shared notes from the main inventory, shared-document inventory, and every
  returned folder's document IDs; overlapping records are deduplicated by ID.
- Full note bodies, metadata, current and original summary panel JSON, transcript
  segments including timestamps, audio sources, and any returned speaker metadata.
- Inline images referenced by notes/panels on Granola, its known CloudFront
  distributions, and Amazon S3 hosts.
- Available folder metadata, recipes, templates, workspaces, preferences, selected
  calendars, calendar events, people, action items, and pre-meeting briefs.
- Original inventory pages and payloads for inspection, including deletion metadata.

```text
exports/granola/
├── manifest.json          Coverage, failures, provenance, and per-resource status
├── documents.json         Merged document inventory
├── notes/<id>.json        Full note record
├── transcript/<id>.json   Transcript segments
├── panels/<id>.json       AI summaries (original and edited)
├── meetings/<id>.md       Readable notes, summaries, and transcript
├── meetings/INDEX.md      Chronological index with transcript status
├── attachments/           Downloaded inline images
├── aux/                   Account and folder data
└── raw/                   Original API responses
```

Files use stable IDs so duplicate titles and renamed meetings do not collide.
JSON is the authoritative copy; Markdown is a readable rendering and may not
reproduce every editor node. Downloaded images have relative links in Markdown;
the manifest maps their original URLs to local paths. Other image URLs remain remote.

## Authentication alternatives

Older Granola releases keep an unencrypted `supabase.json` in their application
data directory. The CLI reads its WorkOS session automatically. It also accepts
`--auth-file /path/to/supabase.json` or the `GRANOLA_ACCESS_TOKEN` environment
variable (`--token-env NAME` selects another variable). Environment tokens take
precedence over session files. Never put tokens in command-line arguments or Git.

Current encrypted desktop files are not decrypted or modified. `--desktop-auth`
uses the already signed-in app instead. On expiry or HTTP 401, the export stops
with a saved manifest; authenticate again and rerun. The exporter does not rotate
refresh tokens or write to Granola's authentication files. A `grn_` Personal API
key is not a desktop session token and is not supported by this backend.

## Reruns and incomplete exports

Normal runs rediscover everything and fetch fresh resources. To resume a failed
or interrupted run more quickly:

```sh
uv run granola-export export --desktop-auth --resume --output ~/Backups/granola
```

`--resume` reuses successful, nonempty transcript and panel files only when the
document metadata fingerprint is unchanged. Empty, missing, corrupt, and failed
payloads are retried. A transcript may change without its document metadata
changing; omit `--resume` for a fresh backup. No old export files are deleted.
If a resource becomes unavailable, its earlier JSON backup remains on disk, but
the current manifest and Markdown mark it unavailable. Use a new output directory
for separate accounts or point-in-time snapshots.

Exit statuses:

| Code | Meaning |
| --- | --- |
| `0` | Configured export finished without required-resource errors |
| `1` | Authentication, inventory, or filesystem failure |
| `2` | Partial export, or invalid CLI arguments |
| `130` | Interrupted with Ctrl+C |

An empty transcript is recorded as `empty`; a forbidden or missing transcript as
`unavailable`; transport/server failures as `error`. Empty transcripts do not
cause a failing exit status. Optional auxiliary endpoints returning unavailable
or unsupported are recorded without failing the main export. Always inspect
`manifest.json` to see coverage and limitations.

Useful options:

```sh
granola-export export --help
granola-export export --desktop-auth --document-id <note-id>
granola-export export --desktop-auth --no-auxiliary --no-attachments
granola-export export --desktop-auth --delay 0.5 --timeout 60 --page-size 50
```

`--document-id` adds a known note to normal discovery; it does not restrict the
export. Requests are sequential, paced, and retry rate limits and transient
failures with backoff. Authentication is sent only to known Granola API hosts,
never attachment URLs. New export files have owner-only permissions on POSIX.

## Python API

```python
from pathlib import Path

from granola_export.auth import load_token
from granola_export.client import GranolaClient
from granola_export.exporter import Exporter, ExportOptions

with GranolaClient(load_token()) as client:
    result = Exporter(client, ExportOptions(output=Path("exports/granola"))).run()
print(result["status"])
```

For an encrypted desktop session, replace `load_token()` with
`receive_desktop_token(print)` from `granola_export.auth` and run the displayed
command in Granola's developer console.

## Limits

These are private desktop API endpoints and can change. The export includes what
your current account can discover and read. Shared notes do not necessarily grant
transcript access. Deleted or inaccessible content, expired attachment links, and
content never returned by the server cannot be recovered. Link-only notes absent
from inventories need an explicit `--document-id`. The package does not export
audio recordings, local IndexedDB chat history, or every Granola product entity.
It is not a complete account archive supplied by Granola.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

Tests use synthetic data and a mocked HTTP transport. No credentials or real
meeting data are required or included. The reference exporter informed the API
coverage; current shared-document, batch, folder, diarization, and authentication
behavior was also checked against the installed desktop client's code.

The desktop-auth flow and a full own/shared/folder export were verified live on
macOS with Granola Desktop 7.595.3. Tests also cover Python 3.11 and 3.14.
