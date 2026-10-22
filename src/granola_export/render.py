from __future__ import annotations

import re
from typing import Any


def prose_to_markdown(node: Any) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(prose_to_markdown(child) for child in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    text = node.get("text", "")
    if kind == "text":
        for mark in node.get("marks", []):
            mark_type = mark.get("type")
            if mark_type in {"bold", "strong"}:
                text = f"**{text}**"
            elif mark_type in {"italic", "em"}:
                text = f"*{text}*"
            elif mark_type == "code":
                text = f"`{text}`"
            elif mark_type == "link":
                text = f"[{text}]({mark.get('attrs', {}).get('href', '')})"
        return text
    content = prose_to_markdown(node.get("content", []))
    if kind == "heading":
        return f"{'#' * min(6, max(1, int(attrs.get('level', 2))))} {content}\n\n"
    if kind == "paragraph":
        return content + "\n\n"
    if kind in {"bulletList", "bullet_list", "orderedList", "ordered_list"}:
        lines = []
        for index, child in enumerate(node.get("content", []), start=attrs.get("start", 1)):
            prefix = f"{index}. " if kind in {"orderedList", "ordered_list"} else "- "
            parts = prose_to_markdown(child).strip().splitlines() or [""]
            lines.append(prefix + parts[0])
            lines.extend(" " * len(prefix) + line for line in parts[1:])
        return "\n".join(lines) + "\n\n"
    if kind in {"hardBreak", "hard_break"}:
        return "\n"
    if kind == "blockquote":
        return "\n".join("> " + line for line in content.strip().splitlines()) + "\n\n"
    if kind in {"codeBlock", "code_block"}:
        return f"````{attrs.get('language', '') or ''}\n{content}\n````\n\n"
    if kind == "image":
        return f"![{attrs.get('alt', '') or ''}]({attrs.get('src', '')})\n\n"
    if kind in {"horizontalRule", "horizontal_rule"}:
        return "\n---\n\n"
    return text + content


def one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def render_meeting(document: dict, panels: list[dict], transcript: list[dict], state: dict) -> str:
    lines = [f"# {one_line(document.get('title')) or 'Untitled'}", ""]
    for label, value in [
        ("ID", document["id"]),
        ("Created", document.get("created_at")),
        ("Updated", document.get("updated_at")),
        ("Sources", ", ".join(state["sources"])),
    ]:
        if value:
            lines.append(f"- **{label}:** {one_line(value)}")
    lines.extend(["", "## Notes", ""])
    notes = document.get("notes_markdown") or prose_to_markdown(document.get("notes"))
    lines.extend([notes or document.get("notes_plain") or "_No note body returned._", ""])
    attachments = state.get("attachments", [])
    if attachments:
        lines.extend(["## Attachments", ""])
        for index, attachment in enumerate(attachments, 1):
            url = "../" + attachment["path"] if attachment["status"] == "ok" else attachment["url"]
            lines.append(f"- [Image {index}]({url})")
        lines.append("")
    for panel in panels:
        lines.extend(
            [
                f"## {one_line(panel.get('title')) or 'Summary'}",
                "",
                prose_to_markdown(panel.get("content"))
                or prose_to_markdown(panel.get("original_content")),
                "",
            ]
        )
    lines.extend(["## Transcript", ""])
    status = state.get("transcript", {}).get("status", "not_fetched")
    if status == "empty":
        lines.append("_Granola returned an empty transcript._")
    elif status not in {"ok", "cached"}:
        lines.append(f"_Transcript unavailable in this run: {status}. See manifest.json._")
    else:
        for segment in sorted(transcript, key=lambda item: str(item.get("start_timestamp", ""))):
            detected = segment.get("detectedSpeaker") or {}
            speaker = (
                segment.get("detected_speaker_name")
                or (detected.get("participantName") if isinstance(detected, dict) else None)
                or segment.get("speaker")
                or segment.get("speaker_id")
                or segment.get("source")
            )
            if isinstance(speaker, dict):
                speaker = speaker.get("name") or speaker.get("id")
            label = one_line(speaker) or "Unknown speaker"
            stamp = one_line(segment.get("start_timestamp"))
            lines.extend([f"**{label}** [{stamp}]  ", str(segment.get("text", "")), ""])
    markdown = "\n".join(lines).rstrip() + "\n"
    for attachment in attachments:
        if attachment["status"] == "ok":
            markdown = markdown.replace(attachment["url"], "../" + attachment["path"])
    return markdown
