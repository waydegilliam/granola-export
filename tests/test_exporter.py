import json
from collections import Counter

import httpx
import pytest

from granola_export.client import GranolaClient
from granola_export.errors import AuthError, ShapeError
from granola_export.exporter import Exporter, ExportOptions, read_json


class Account:
    def __init__(self):
        self.version = "2026-01-01"
        self.calls = Counter()
        self.denied = set()
        self.empty = set()
        self.expire = False
        self.attachment_auth = []

    def document(self, document_id):
        return {
            "id": document_id,
            "title": "Title / same for everyone",
            "updated_at": self.version,
            "notes_markdown": f"Notes for {document_id}",
            "created_at": "2026-01-01T00:00:00Z",
            "attachments": [
                {
                    "type": "image",
                    "url": "https://d1ywymt16s8sdr.cloudfront.net/image.png?signature=x",
                }
            ],
        }

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        document_id = body.get("document_id")
        self.calls[(path, document_id)] += 1
        if request.method == "GET":
            self.attachment_auth.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=b"image")
        if path == "/v2/get-documents":
            assert body["include_shared_with_me"] is True
            return httpx.Response(
                200,
                json={
                    "docs": [self.document("owned")],
                    "shared": [self.document("shared-in-list")],
                    "deleted": ["deleted-record"],
                },
            )
        if path == "/v1/get-shared-documents":
            return httpx.Response(
                200,
                json={
                    "docs": [
                        self.document("shared-in-list"),
                        self.document("shared-only"),
                    ]
                },
            )
        if path == "/v1/get-document-lists-metadata":
            return httpx.Response(
                200,
                json={
                    "lists": {
                        "folder": {"id": "folder", "document_ids": ["shared-only", "folder-only"]},
                    }
                },
            )
        if path == "/v1/get-documents-batch":
            return httpx.Response(
                200,
                json={
                    "docs": [self.document(document_id) for document_id in body["document_ids"]],
                },
            )
        if path == "/v1/get-document-transcript":
            if self.expire:
                return httpx.Response(401)
            if document_id in self.denied:
                return httpx.Response(403)
            assert body["include_diarization"] is True
            return httpx.Response(
                200,
                json=[]
                if document_id in self.empty
                else [
                    {
                        "text": f"Transcript for {document_id}",
                        "source": "system",
                        "start_timestamp": "2026-01-01T00:00:00Z",
                        "is_final": True,
                    }
                ],
            )
        if path == "/v1/get-document-panels":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "panel",
                        "title": "Summary",
                        "content": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Summary text"}],
                                },
                            ],
                        },
                        "original_content": {"type": "doc", "content": []},
                    }
                ],
            )
        return httpx.Response(404)


def run(account, tmp_path, **options):
    with GranolaClient("secret", transport=httpx.MockTransport(account.handle), delay=0) as client:
        return Exporter(
            client, ExportOptions(tmp_path, auxiliary=False, **options), lambda _: None
        ).run()


def test_export_covers_owned_shared_and_folder_only_notes(tmp_path):
    account = Account()
    manifest = run(account, tmp_path)
    assert manifest["status"] == "complete"
    assert manifest["document_count"] == manifest["exported_count"] == 4
    for document_id in ("owned", "shared-only", "shared-in-list", "folder-only"):
        assert (tmp_path / "notes" / f"{document_id}.json").exists()
        markdown = (tmp_path / "meetings" / f"{document_id}.md").read_text()
        assert f"Transcript for {document_id}" in markdown
        assert f"Notes for {document_id}" in markdown
        assert "Summary text" in markdown
        assert "../attachments/" in markdown
        assert account.calls[("/v1/get-document-transcript", document_id)] == 1
    assert manifest["documents"]["folder-only"]["sources"] == ["batch", "folder:folder"]
    assert read_json(tmp_path / "raw/discovery/documents-00000.json")["deleted"] == [
        "deleted-record"
    ]
    assert account.attachment_auth and all(header is None for header in account.attachment_auth)
    assert len(list((tmp_path / "attachments").iterdir())) == 1
    assert "secret" not in (tmp_path / "manifest.json").read_text()
    assert (tmp_path / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_denied_and_empty_transcripts_are_distinct_and_retried(tmp_path):
    account = Account()
    account.denied.add("shared-only")
    account.empty.add("folder-only")
    first = run(account, tmp_path)
    assert first["status"] == "partial"
    assert first["documents"]["shared-only"]["transcript"]["status"] == "unavailable"
    assert first["documents"]["folder-only"]["transcript"]["status"] == "empty"
    assert not (tmp_path / "transcript/shared-only.json").exists()
    account.denied.clear()
    account.empty.clear()
    second = run(account, tmp_path, resume=True)
    assert second["status"] == "complete"
    assert second["documents"]["owned"]["transcript"]["status"] == "cached"
    assert second["documents"]["shared-only"]["transcript"]["status"] == "ok"
    assert second["documents"]["folder-only"]["transcript"]["status"] == "ok"
    assert account.calls[("/v1/get-document-transcript", "owned")] == 1
    assert account.calls[("/v1/get-document-transcript", "shared-only")] == 2


def test_resume_refetches_changed_and_corrupt_payloads(tmp_path):
    account = Account()
    run(account, tmp_path)
    (tmp_path / "transcript/owned.json").write_text('{"error":"old failure"}')
    run(account, tmp_path, resume=True)
    assert account.calls[("/v1/get-document-transcript", "owned")] == 2
    account.version = "2026-02-01"
    run(account, tmp_path, resume=True)
    assert account.calls[("/v1/get-document-transcript", "owned")] == 3
    assert len(list((tmp_path / "meetings").glob("*.md"))) == 5


def test_expired_auth_saves_interrupted_manifest(tmp_path):
    account = Account()
    account.expire = True
    with pytest.raises(AuthError):
        run(account, tmp_path)
    manifest = read_json(tmp_path / "manifest.json")
    assert manifest["status"] == "interrupted"
    assert manifest["document_count"] == 4
    assert manifest["exported_count"] == 0


def test_lost_access_preserves_previous_json_but_marks_current_markdown(tmp_path):
    account = Account()
    run(account, tmp_path)
    original = (tmp_path / "transcript/owned.json").read_bytes()
    account.denied.add("owned")
    result = run(account, tmp_path)
    assert result["status"] == "partial"
    assert (tmp_path / "transcript/owned.json").read_bytes() == original
    assert "Transcript unavailable" in (tmp_path / "meetings/owned.md").read_text()


def test_malformed_inventory_is_not_a_successful_empty_export(tmp_path):
    with (
        GranolaClient(
            "secret",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"wrong": []})),
            delay=0,
        ) as client,
        pytest.raises(ShapeError, match="Expected a collection"),
    ):
        Exporter(client, ExportOptions(tmp_path), lambda _: None).run()
    assert read_json(tmp_path / "manifest.json")["status"] == "interrupted"


def test_full_note_response_can_clear_previously_listed_fields(tmp_path):
    with GranolaClient("secret", delay=0) as client:
        exporter = Exporter(client, ExportOptions(tmp_path))
        exporter.add_document({"id": "x", "notes_plain": "Old notes"}, "documents")
        exporter.add_document({"id": "x", "notes_plain": None}, "batch")
    assert exporter.documents["x"]["notes_plain"] is None
