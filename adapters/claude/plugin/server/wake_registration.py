"""Acknowledge a received wake check through the session’s saved pairing."""
import sys
import uuid
from urllib.parse import urlsplit


def service_id(pairing):
    service = urlsplit(pairing["endpoint"]).path.split("/")[-2]
    if str(uuid.UUID(service)) != service.lower():
        raise ValueError("Invalid D-Bond service address")
    return service


def acknowledge(alias, sequence):
    if type(sequence) is not int or not 1 <= sequence <= 2**63 - 1:
        raise ValueError("Expected the positive broker sequence of the received wake check")
    # The packaged executable is named dbond, without a .py suffix.
    cli = sys.modules.get("dbond") or sys.modules["__main__"]
    if not hasattr(cli, "_read_pairing"):
        import dbond as cli
    pairing = cli._read_pairing(alias)
    code, reply = cli._request(pairing, "POST", cli._direct_url(pairing) + "/wake-ack",
                               {"sequence": sequence})
    if (code != 200 or not isinstance(reply, dict) or reply.get("state") != "verified" or reply.get("sequence") != sequence
            or type(reply.get("sequence")) is not int or type(reply.get("duplicate")) is not bool
            or not isinstance(reply.get("id"), str) or not cli.WAKE_ID_RE.fullmatch(reply["id"])):
        raise ValueError(f"Wake check {sequence} was not acknowledged by this pairing")
    return {"state": "verified", "sequence": sequence, "id": reply["id"],
            "duplicate": reply["duplicate"], "connection_id": service_id(pairing)}
