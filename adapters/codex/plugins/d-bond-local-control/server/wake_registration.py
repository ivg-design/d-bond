"""Broker wake test client. Only a receiver that obtains the private challenge answers it."""
import re
import sys
import uuid
from urllib.parse import urlsplit

import owner


def service_id(pairing):
    service = urlsplit(pairing["endpoint"]).path.split("/")[-2]
    if str(uuid.UUID(service)) != service.lower():
        raise ValueError("Invalid D-Bond service address")
    return service


def begin(pairing):
    try:
        reply = owner.request({"op": "begin_wake_test", "id": service_id(pairing)})
        state = reply.get("wakeState", reply.get("state"))
        if state not in ("offline", "pending", "verified"):
            raise ValueError("The broker did not return a wake test state")
        result = {"state": state}
        if reply.get("lastVerifiedAt"):
            result["lastVerifiedAt"] = reply["lastVerifiedAt"]
        return result
    except (OSError, ValueError, RuntimeError):
        return {"state": "unknown", "reason": "The installed broker could not start a wake test. Check that D-Bond is running and up to date."}


def acknowledge(alias, token):
    if not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,160}", token):
        raise ValueError("Invalid wake challenge format")
    # The packaged executable is named dbond, without a .py suffix.
    cli = sys.modules.get("dbond") or sys.modules["__main__"]
    if not hasattr(cli, "_read_pairing"):
        import dbond as cli
    pairing = cli._read_pairing(alias)
    code, reply = cli._request(pairing, "POST", cli._direct_url(pairing) + "/wake-ack", {"token": token})
    if code != 200 or reply.get("wakeState", reply.get("state")) != "verified":
        raise ValueError("This connection could not verify its current wake test")
    return {"state": "verified", "connection_id": service_id(pairing)}
