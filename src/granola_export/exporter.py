from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from granola_export.client import GranolaClient, records
from granola_export.errors import ApiError, AuthError, ExportError, ShapeError
from granola_export.render import one_line, render_meeting

AUXILIARY_ENDPOINTS = {
    "recipes": ("https://api.granola.ai", "/v1/get-recipes"),
    "panel_templates": ("https://api.granola.ai", "/v1/get-panel-templates"),
    "workspaces": ("https://api.granola.ai", "/v1/get-workspaces"),
    "user_preferences": ("https://api.granola.ai", "/v1/get-user-preferences"),
    "selected_calendars": ("https://api.granola.ai", "/v1/get-selected-calendars"),
    "calendar_events": ("https://api.granola.ai", "/v1/get-google-events"),
    "people": ("https://pecan.api.granola.ai", "/v1/get-people"),
    "action_items": ("https://maple.api.granola.ai", "/v1/get-action-items"),
    "pre_meeting_briefs": ("https://maple.api.granola.ai", "/v1/get-pre-meeting-briefs"),
}


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".export-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def safe_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ShapeError("An API record has an invalid or unsafe ID.")
    return value


def fingerprint(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ExportOptions:
    output: Path
    page_size: int = 100
    resume: bool = False
    auxiliary: bool = True
    attachments: bool = True
    document_ids: tuple[str, ...] = ()


class Exporter:
    def __init__(
        self,
        client: GranolaClient,
        options: ExportOptions,
        progress: Callable[[str], None] = print,
    ):
        self.client = client
        self.options = options
        self.root = options.output.expanduser().resolve()
        self.progress = progress
        self.documents = {}
        self.sources = {}
        previous = read_json(self.root / "manifest.json")
        self.previous = previous if isinstance(previous, dict) else {}
        self.manifest = {
            "schema_version": 1,
            "started_at": timestamp(),
            "status": "running",
            "discovery": {},
            "documents": {},
            "auxiliary": {},
            "errors": [],
            "options": {
                "resume": options.resume,
                "auxiliary": options.auxiliary,
                "attachments": options.attachments,
            },
        }

    def save(self) -> None:
        write_json(self.root / "manifest.json", self.manifest)

    def add_document(self, document: dict, source: str) -> None:
        document = dict(document)
        document_id = safe_id(document.get("id", document.get("document_id")))
        document["id"] = document_id
        existing = self.documents.setdefault(document_id, {})
        existing.update(
            document
            if source == "batch"
            else {key: value for key, value in document.items() if value is not None}
        )
        self.sources.setdefault(document_id, set()).add(source)

    def record_error(self, stage: str, error: ExportError) -> None:
        self.manifest["errors"].append({"stage": stage, "message": str(error)})
        self.progress(f"  Incomplete: {stage}: {error}")

    def discover(self) -> None:
        self.progress("Discovering own and shared meetings...")
        for index, page in enumerate(
            self.client.pages(
                "/v2/get-documents",
                "docs",
                "documents",
                body={"include_shared_with_me": True},
                page_size=self.options.page_size,
            )
        ):
            write_json(self.root / "raw/discovery" / f"documents-{index:05}.json", page)
            for document in records(page, "docs", "documents"):
                self.add_document(document, "documents")
            if isinstance(page, dict) and "shared" in page:
                for document in records(page, "shared"):
                    self.add_document(document, "shared")
        self.manifest["discovery"]["documents"] = "ok"
        try:
            for index, page in enumerate(
                self.client.pages(
                    "/v1/get-shared-documents",
                    "shared",
                    "docs",
                    "documents",
                    "shared_documents",
                    page_size=self.options.page_size,
                )
            ):
                write_json(self.root / "raw/discovery" / f"shared-{index:05}.json", page)
                for document in records(page, "shared", "docs", "documents", "shared_documents"):
                    self.add_document(document, "shared")
            self.manifest["discovery"]["shared"] = "ok"
        except ApiError as error:
            if error.status in {404, 405}:
                self.manifest["discovery"]["shared"] = "unsupported; requested via get-documents"
            else:
                self.record_error("shared discovery", error)
        except ShapeError as error:
            self.record_error("shared discovery", error)
        self.discover_folders()
        for document_id in self.options.document_ids:
            self.add_document({"id": document_id}, "explicit")
        self.progress(f"Discovered {len(self.documents)} unique meetings/notes.")
        write_json(self.root / "documents.json", list(self.documents.values()))
        self.save()

    def discover_folders(self) -> None:
        try:
            try:
                data = self.client.post(
                    "/v1/get-document-lists-metadata", {"include_document_ids": True}
                )
            except ApiError as error:
                if error.status not in {404, 405}:
                    raise
                data = self.client.post("/v2/get-document-lists", {"include_document_ids": True})
            write_json(self.root / "aux/folders.json", data)
            folders = records(data, "lists", "folders")
            for folder in folders:
                folder_id = safe_id(folder.get("id"))
                source = f"folder:{folder_id}"
                if "document_ids" in folder:
                    document_ids = folder["document_ids"]
                    if not isinstance(document_ids, list):
                        raise ShapeError("Folder document_ids must be an array.")
                    for document_id in document_ids:
                        self.add_document({"id": document_id}, source)
                elif "documents" in folder or "docs" in folder:
                    for document in records(folder, "documents", "docs"):
                        self.add_document(document, source)
                else:
                    raise ShapeError(
                        "Folder inventory has no document IDs; membership is incomplete."
                    )
            self.manifest["discovery"]["folders"] = "ok"
        except (ApiError, ShapeError) as error:
            self.record_error("folder discovery", error)

    def hydrate(self) -> None:
        ids = list(self.documents)
        self.progress("Fetching full note bodies, including shared folder notes...")
        for offset in range(0, len(ids), 50):
            batch = ids[offset : offset + 50]
            try:
                data = self.client.post("/v1/get-documents-batch", {"document_ids": batch})
                write_json(self.root / "raw/discovery" / f"batch-{offset:05}.json", data)
                documents = records(data, "docs", "documents")
                found = set()
                for document in documents:
                    self.add_document(document, "batch")
                    found.add(safe_id(document.get("id", document.get("document_id"))))
                for document_id in set(batch) - found:
                    self.record_error(
                        f"document:{document_id}", ShapeError("Full note body was not returned.")
                    )
            except (ApiError, ShapeError) as error:
                self.record_error(f"document batch:{offset}", error)
        write_json(self.root / "documents.json", list(self.documents.values()))

    def fetch_resource(self, document: dict, kind: str, state: dict) -> list[dict]:
        document_id = document["id"]
        path = self.root / kind / f"{document_id}.json"
        previous = self.previous.get("documents", {}).get(document_id, {})
        cached = read_json(path) if self.options.resume else None
        if (
            isinstance(cached, list)
            and cached
            and all(isinstance(item, dict) for item in cached)
            and previous.get("fingerprint") == state["fingerprint"]
            and previous.get(kind, {}).get("status") in {"ok", "cached"}
        ):
            state[kind] = {"status": "cached", "count": len(cached)}
            return cached
        try:
            body = {"document_id": document_id}
            if kind == "transcript":
                body["include_diarization"] = True
            data = self.client.post(f"/v1/get-document-{kind}", body)
            items = records(data, kind, "segments" if kind == "transcript" else "panels")
            write_json(self.root / "raw" / kind / f"{document_id}.json", data)
            write_json(path, items)
            state[kind] = {"status": "ok" if items else "empty", "count": len(items)}
            return items
        except (ApiError, ShapeError) as error:
            denied = isinstance(error, ApiError) and error.status in {403, 404}
            state[kind] = {"status": "unavailable" if denied else "error", "error": str(error)}
            self.record_error(f"{kind}:{document_id}", error)
            return []

    def export_document(self, document: dict) -> None:
        document_id = document["id"]
        state = {
            "sources": sorted(self.sources[document_id]),
            "fingerprint": fingerprint(document),
            "title": document.get("title"),
        }
        self.manifest["documents"][document_id] = state
        write_json(self.root / "notes" / f"{document_id}.json", document)
        transcript = self.fetch_resource(document, "transcript", state)
        panels = self.fetch_resource(document, "panels", state)
        if self.options.attachments:
            self.export_attachments(document, panels, state)
        path = self.root / "meetings" / f"{document_id}.md"
        atomic_write(path, render_meeting(document, panels, transcript, state).encode())
        state["markdown"] = str(path.relative_to(self.root))
        self.save()

    def export_attachments(self, document: dict, panels: list[dict], state: dict) -> None:
        urls = set()

        def visit(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, dict):
                if value.get("type") == "image" or "mime_type" in value or "mimeType" in value:
                    attrs = value.get("attrs") or value
                    for key in ("url", "src", "download_url"):
                        if isinstance(attrs.get(key), str):
                            urls.add(attrs[key])
                for item in value.values():
                    visit(item)

        visit(document)
        visit(panels)
        results = []
        for url in sorted(urls):
            parsed = httpx.URL(url)
            host = parsed.host
            allowed = parsed.scheme == "https" and any(
                host == domain or host.endswith("." + domain)
                for domain in (
                    "granola.ai",
                    "granola.so",
                    "amazonaws.com",
                    "d1ywymt16s8sdr.cloudfront.net",
                    "d2y2qtsuzmwn83.cloudfront.net",
                )
            )
            if not allowed:
                results.append({"url": url, "status": "external_url_preserved"})
                continue
            name = hashlib.sha256(url.split("?")[0].encode()).hexdigest()
            suffix = Path(parsed.path).suffix
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
                suffix = ".bin"
            path = self.root / "attachments" / f"{name}{suffix}"
            try:
                if not (self.options.resume and path.exists()):
                    response = self.client.http.get(url)
                    response.raise_for_status()
                    atomic_write(path, response.content)
                results.append(
                    {"url": url, "status": "ok", "path": str(path.relative_to(self.root))}
                )
            except httpx.HTTPError:
                results.append({"url": url, "status": "error"})
                self.record_error("attachment", ExportError("Attachment download failed."))
        state["attachments"] = results

    def export_auxiliary(self) -> None:
        self.progress("Exporting auxiliary account data...")
        for name, (base, endpoint) in AUXILIARY_ENDPOINTS.items():
            try:
                data = self.client.post(endpoint, base=base)
                write_json(self.root / "aux" / f"{name}.json", data)
                self.manifest["auxiliary"][name] = {"status": "ok"}
            except (ApiError, ShapeError) as error:
                unavailable = isinstance(error, ApiError) and error.status in {403, 404, 405, 501}
                self.manifest["auxiliary"][name] = {
                    "status": "unavailable" if unavailable else "error",
                    "error": str(error),
                }
                if not unavailable:
                    self.record_error(f"auxiliary:{name}", error)

    def write_index(self) -> None:
        lines = ["# Granola export", "", f"Export status: {self.manifest['status']}", ""]
        for document in sorted(
            self.documents.values(), key=lambda item: str(item.get("created_at", "")), reverse=True
        ):
            document_id = document["id"]
            state = self.manifest["documents"].get(document_id, {})
            title = one_line(document.get("title")) or document_id
            title = title.replace("[", "\\[").replace("]", "\\]")
            date = one_line(document.get("created_at"))[:10]
            transcript = state.get("transcript", {}).get("status", "not_fetched")
            link = f"[{title}]({document_id}.md)" if state.get("markdown") else title
            lines.append(f"- {date} {link} — transcript: {transcript}")
        atomic_write(self.root / "meetings/INDEX.md", ("\n".join(lines) + "\n").encode())

    def run(self) -> dict:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.save()
        try:
            self.discover()
            self.hydrate()
            total = len(self.documents)
            for index, document in enumerate(list(self.documents.values()), 1):
                if index == 1 or index % 10 == 0 or index == total:
                    self.progress(f"Exporting meeting {index}/{total}...")
                self.export_document(document)
            if self.options.auxiliary:
                self.export_auxiliary()
            self.manifest["status"] = "partial" if self.manifest["errors"] else "complete"
        except (AuthError, ExportError, OSError, KeyboardInterrupt):
            self.manifest["status"] = "interrupted"
            raise
        finally:
            self.manifest["finished_at"] = timestamp()
            self.manifest["document_count"] = len(self.documents)
            self.manifest["exported_count"] = sum(
                "markdown" in state for state in self.manifest["documents"].values()
            )
            self.manifest["summary"] = {
                resource: dict(
                    Counter(
                        self.manifest["documents"]
                        .get(document_id, {})
                        .get(resource, {})
                        .get("status", "not_fetched")
                        for document_id in self.documents
                    )
                )
                for resource in ("transcript", "panels")
            }
            self.save()
            self.write_index()
        return self.manifest
