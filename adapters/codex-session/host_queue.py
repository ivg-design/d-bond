"""Format broker messages for inline delivery to an existing Codex chat."""
from __future__ import annotations

import re

UUID_RE = r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"


def inline_message(alias: str, lane: str, event: dict, *, retained: bool = False,
                   sender_name: str | None = None) -> str:
    """Put the original text last so Markdown, code and whitespace survive delivery."""
    sender, message_id = event["from"], event["id"]
    sequence, body = event["sequence"], event["text"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", alias):
        raise ValueError("Invalid pairing alias")
    if type(sequence) is not int or sequence < 1 or not isinstance(body, str) or not body.strip():
        raise ValueError("Invalid message")
    wake = lane == "direct" and sender == "owner" and re.fullmatch(r"@wake:" + UUID_RE, message_id)
    if lane == "direct":
        if not (wake or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}", message_id)):
            raise ValueError("Invalid direct message ID")
        if sender != "owner" and not re.fullmatch(UUID_RE, sender):
            raise ValueError("Invalid direct sender")
    elif lane != "dot" or sender != "dot" or not re.fullmatch(r"@dot:[0-9]+", message_id):
        raise ValueError("Invalid Dot message")

    display = " ".join(sender_name.split())[:160] if sender_name else ""
    origin = f"{display} ({sender})" if display and display != sender else sender
    fields = [f"D-Bond {'retained ' if retained else ''}message from {origin}",
              f"connection {alias}", f"message {message_id}", f"sequence {sequence}"]
    if event.get("in_reply_to"):
        fields.append(f"in reply to {event['in_reply_to']}")
    header = "[" + " · ".join(fields) + "]"
    if wake:
        header += f"\nAcknowledge once with ~/.local/bin/dbond wake-ack {alias} {sequence}."
    return header + "\n\n" + body
