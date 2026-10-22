from granola_export.render import prose_to_markdown, render_meeting


def test_nested_bullet_lists_and_unknown_nodes_preserve_text():
    node = {
        "type": "bulletList",
        "content": [
            {
                "type": "listItem",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Parent"}]},
                    {
                        "type": "bulletList",
                        "content": [
                            {
                                "type": "listItem",
                                "content": [
                                    {
                                        "type": "newNode",
                                        "content": [{"type": "text", "text": "Child"}],
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    markdown = prose_to_markdown(node)
    assert "- Parent" in markdown
    assert "  - Child" in markdown


def test_transcript_speaker_sources_are_not_mislabeled_as_user():
    markdown = render_meeting(
        {"id": "x", "title": "Shared"},
        [],
        [
            {"source": "microphone", "text": "Owner's speech", "start_timestamp": "2026-01-01"},
        ],
        {"sources": ["shared"], "transcript": {"status": "ok"}},
    )
    assert "**microphone**" in markdown
    assert "**You**" not in markdown


def test_current_desktop_speaker_names_are_rendered():
    markdown = render_meeting(
        {"id": "x"},
        [],
        [
            {"detected_speaker_name": "Alice", "source": "system", "text": "Hello"},
            {"detectedSpeaker": {"participantName": "Bob"}, "text": "Hi"},
        ],
        {"sources": ["shared"], "transcript": {"status": "ok"}},
    )
    assert "**Alice**" in markdown
    assert "**Bob**" in markdown
