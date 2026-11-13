#!/usr/bin/env python3
"""Generic owner-local D-Bond direct messaging over MCP stdio.

The tool server uses the same private CLI pairings and loopback HTTP authority.
Registration attaches the host adapter for an identified local Codex chat.
"""
import contextlib
import io
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))
import dbond  # noqa: E402
import codex_registration
import wake_registration
import owner  # noqa: E402


def tool(name, description, properties, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required), "additionalProperties": False}}


STRING = {"type": "string"}
TOOLS = [
    tool("local_status", "Check whether the D-Bond Mac app is installed and its owner service is reachable. Read-only; no pairing or credentials required.", {}),
    tool("session_status", "Read owner-observed subscriptions by default. Set finish_registration=true only after the registration listener attaches to schedule one delayed proof, then END setup and handle the owner wake check in a later turn. Repeated finish is broker-idempotent. Subscriber presence alone is not proof of a host turn.",
         {"alias": STRING, "finish_registration": {"type": "boolean"}}, ("alias",)),
    tool("enroll_agent", "Register this session for two-way messages. For Codex pass this host chat’s exact thread_id and name. Reuse its pairing, attach its listener, and start the wake check in this turn. Report the broker's verified result before calling it ready. Keep any pending or unsupported host status visible.",
         {"alias": STRING, "name": STRING, "thread_id": STRING, "host": {"type":"string","enum":["codex","other"]}, "direct_only": {"type":"boolean"}}, ("name",)),
    tool("acknowledge_wake", "Acknowledge the wake check delivered to this session using its broker sequence.",
         {"alias": STRING, "sequence": {"type": "integer", "minimum": 1}}, ("alias", "sequence")),
    tool("list_direct_peers", "List active or paused direct recipients by stable connection ID and display name.",
         {"alias": STRING}, ("alias",)),
    tool("send_direct_message", "Send directly to a paired recipient; broker receipt is not an answer. Reuse message_id on retry.",
         {"alias": STRING, "to": STRING, "text": STRING, "message_id": STRING}, ("alias", "to", "text")),
    tool("receive_direct_messages", "Read durable inbound messages after a sequence; invoke again or hold a direct-listen stream for future messages.",
         {"alias": STRING, "after": {"type": "integer", "minimum": 0}}, ("alias",)),
    tool("answer_direct_message", "Answer a received direct message; to must be its exact sender ID.",
         {"alias": STRING, "to": STRING, "in_reply_to": STRING, "text": STRING, "message_id": STRING},
         ("alias", "to", "in_reply_to", "text")),
    tool("ask_dot", "Ask Dot, optionally on behalf of an addressed recipient. When the user asks for a plain notification via Herald, pass the active Herald / Notifications connection in to so Dot receives its owner rule.",
         {"alias": STRING, "text": STRING, "to": STRING, "message_id": STRING,
          "timeout": {"type": "integer", "minimum": 1, "maximum": 300}}, ("alias", "text")),
    tool("wait_for_dot_push", "Wait for one authenticated Dot push while this tool call is active; does not wake an idle desktop chat.",
         {"alias": STRING, "timeout": {"type": "integer", "minimum": 1, "maximum": 60}}, ("alias",)),
    tool("answer_dot_push", "Answer a recorded Dot @dot source ID through the existing correlated route.",
         {"alias": STRING, "source_id": STRING, "text": STRING,
          "timeout": {"type": "integer", "minimum": 1, "maximum": 300}}, ("alias", "source_id", "text")),
]


def _codex_alias(thread):
    """Resolve one exact owner-local binding before choosing a new alias."""
    matches = []
    for path in dbond._safe_dir(create=True).glob("*.json"):
        if path.stem.startswith("request-") or not dbond.ALIAS_RE.fullmatch(path.stem):
            continue
        try:
            pairing = dbond._read_pairing(path.stem)
        except SystemExit:
            continue
        if pairing.get("client") == "codex" and pairing.get("session_id") == thread:
            matches.append(path.stem)
    if len(matches) > 1:
        raise ValueError("Multiple pairings are bound to this Codex chat; resolve them in D-Bond before enrolling")
    return matches[0] if matches else None


def _available_codex_title(title, thread, rows):
    used = {row.get("name", "").casefold() for row in rows if isinstance(row, dict)
            and isinstance(row.get("name"), str)}
    if title.casefold() not in used:
        return title
    for length in (8, 12, 16, 36):
        suffix = " · " + thread[:length]
        candidate = title[:200 - len(suffix)] + suffix
        if candidate.casefold() not in used:
            return candidate
    raise ValueError("Could not choose a unique Codex connection name")


def invoke(name, args):
    if name == "local_status":
        if args:
            raise ValueError("local_status takes no arguments")
        return owner.local_status()
    if name == "session_status":
        return dbond.session_status(args["alias"], finish_registration=args.get("finish_registration", False))
    if name == "acknowledge_wake":
        return wake_registration.acknowledge(args["alias"], args.get("sequence"))
    if name == "enroll_agent":
        thread = None if args.get("host") == "other" else codex_registration.thread_from_request(args)
        explicit_alias = args.get("alias")
        bound_alias = _codex_alias(thread) if thread else None
        if explicit_alias and bound_alias and explicit_alias != bound_alias:
            raise ValueError("This Codex chat is already bound to a different alias; use its existing alias")
        alias = explicit_alias or bound_alias or ("codex-" + thread if thread else None)
        if not alias or not dbond.ALIAS_RE.fullmatch(alias):
            raise ValueError("Provide this session’s alias or exact Codex thread_id")
        title = args["name"]
        if thread and not title.startswith("Codex / "):
            raise ValueError("Use Codex / followed by this chat’s title for a Codex registration")
        if title.startswith("Codex / ") and not thread:
            raise ValueError("Pass this Codex chat’s exact thread_id to attach its listener")
        rows = owner.request({"op":"list"})["services"]
        if dbond._alias_path(alias).exists():
            pairing = dbond._read_pairing(alias)
            matches = [r for r in rows if r.get("id") == wake_registration.service_id(pairing)]
            service = matches[0] if len(matches) == 1 else None
            if pairing.get("session_id") and (pairing.get("client"), pairing.get("session_id")) != ("codex", thread):
                raise ValueError("This alias is bound to a different host session")
            same_codex_chat = bool(thread and service and service.get("name", "").startswith("Codex / ")
                                   and (bound_alias == alias or alias == f"codex-{thread}"))
            if not service or (thread and not same_codex_chat) or (not thread and service["name"] != title):
                raise ValueError("This alias is already bound to a different service or topic")
            if service.get("paused"):
                return {"alias":alias,"state":"paused","reason":"Resume this pairing in D-Bond to activate it."}
            result = {"linked":alias,"reused":True}
        else:
            capture = io.StringIO()
            if thread:
                title = _available_codex_title(title, thread, rows)
            values = {"alias":alias, "name":title, "direct":args.get("direct_only",False), "chrome_id":None,
                      "client":None,"service":None,"topic":None,"session_id":None,"title":None}
            with contextlib.redirect_stdout(capture):
                dbond.command_enroll(type("Args", (), values)())
            result = json.loads(capture.getvalue().splitlines()[0])
            pairing = dbond._read_pairing(alias)
        if thread:
            dbond.bind_session(alias, "codex", thread)
            result["listener"] = codex_registration.attach(alias,thread,pairing)
            ready = result["listener"].get("ready") is True
            status = dbond.session_status(alias, finish_registration=ready)
            result["wake"] = dbond.registration_wake_status(status)
            if ready:
                result["registration_finish"] = status.get("registration_finish")
            result["instructions"] = "End setup after attachment. A verified pairing is ready. If a wake check is pending, acknowledge its sequence when it arrives in the receiving turn."
        return result
    if name in ("ask_dot", "answer_dot_push"):
        timeout = args.get("timeout", 120)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError("Invalid Dot wait timeout")
        capture = io.StringIO()
        values = {"alias": args["alias"], "timeout": timeout}
        if name == "ask_dot":
            values.update(question=args["text"], to=args.get("to"), event_id=args.get("message_id"))
            operation = dbond.command_ask
        else:
            values.update(dot_id=args["source_id"], text=args["text"])
            operation = dbond.command_answer
        timed_out = False
        with contextlib.redirect_stdout(capture):
            try:
                operation(type("Args", (), values)())
            except SystemExit as exc:
                if exc.code != 3:
                    raise
                timed_out = True
        output = json.loads(capture.getvalue().splitlines()[-1])
        if timed_out:
            output["completed"] = False
        return output
    if name == "wait_for_dot_push":
        timeout = args.get("timeout", 30)
        if type(timeout) is not int or not 1 <= timeout <= 60:
            raise ValueError("Invalid Dot push wait timeout")
        pairing = dbond._read_pairing(args["alias"])
        cursor = pairing.get("cursor", 0)
        stream = dbond.Stream(pairing, cursor)
        try:
            deadline = time.monotonic() + timeout
            while True:
                event = stream.next_event(deadline)
                if event is None:
                    return {"state": "no_push", "after": cursor}
                cursor = pairing["cursor"] = event["sequence"]
                if event.get("state") == "dot_push" and re.fullmatch(r"@dot:[0-9]+", event.get("id", "")):
                    pushes = pairing.setdefault("pushes", [])
                    if event["id"] not in pushes:
                        pushes.append(event["id"])
                    pairing["pushes"] = pushes[-256:]
                    dbond._save(args["alias"], pairing)
                    return {"state": "dot_push", "id": event["id"], "sequence": cursor,
                            "text": event.get("reply", "")}
                dbond._save(args["alias"], pairing)
        finally:
            stream.close()
    pairing = dbond._read_pairing(args["alias"])
    if name == "list_direct_peers":
        return {"services": dbond._direct_peers(pairing)}
    if name in ("send_direct_message", "answer_direct_message"):
        target = dbond._direct_target(pairing, args["to"])
        message_id = args.get("message_id") or str(uuid.uuid4())
        if not dbond.EVENT_RE.fullmatch(message_id) or message_id.startswith("@dot:"):
            raise ValueError("Invalid direct message ID")
        body = {"id": message_id, "to": target, "text": args["text"]}
        if name == "answer_direct_message":
            body["in_reply_to"] = args["in_reply_to"]
        status, receipt = dbond._request(pairing, "POST", dbond._direct_url(pairing), body)
        if status not in (200, 202) or receipt.get("id") != message_id:
            raise ValueError("Invalid direct message receipt")
        return {"id": message_id, "to": target, "sequence": receipt["sequence"],
                "duplicate": receipt["duplicate"], "state": "broker_recorded"}
    if name == "receive_direct_messages":
        after = args.get("after", 0)
        if type(after) is not int or after < 0:
            raise ValueError("after must be a nonnegative integer")
        status, result = dbond._request(pairing, "GET", dbond._direct_url(pairing) + "?after=" + str(after), timeout=15)
        if status != 200 or not isinstance(result.get("messages"), list):
            raise ValueError("Invalid direct inbox response")
        return result
    raise ValueError("Unknown tool")


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            method, rid = request.get("method"), request.get("id")
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "d-bond-direct", "version": "0.1.0"},
                          "instructions": "D-Bond supports correlated Dot requests/replies and separate direct peer messages. A direct send receipt is not an agent answer. For a local Codex chat, enroll_agent attaches a per-chat listener and requests a wake check in the same operation. Use session_status to see the result. An ordinary receive tool does not wake an idle chat."}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params") or {}
                name, args = params.get("name"), params.get("arguments") or {}
                if name not in {item["name"] for item in TOOLS} or not isinstance(args, dict):
                    raise ValueError("Unknown tool or invalid arguments")
                result = {"content": [{"type": "text", "text": json.dumps(invoke(name, args), ensure_ascii=False)}]}
            elif method == "notifications/initialized":
                continue
            else:
                raise ValueError("Unknown method")
            if rid is not None:
                emit({"jsonrpc": "2.0", "id": rid, "result": result})
        except (ValueError, KeyError, TypeError, SystemExit, OSError) as exc:
            if isinstance(request, dict) and request.get("id") is not None:
                emit({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": str(exc)}})


if __name__ == "__main__":
    main()
