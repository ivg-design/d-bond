#!/usr/bin/env python3
"""Opt-in Developer ID signing and resumable notarization for a finished app.

No account, identity, profile, private key, or credential is stored in source.
Signing is local except Apple's secure timestamp service. Only the explicit
notarize action uploads an app, and it requires the exact bundle digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe",
         b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}


def command(args: list, *, allow_failure=False) -> subprocess.CompletedProcess:
    result = subprocess.run([str(value) for value in args], capture_output=True, text=True)
    if result.returncode and not allow_failure:
        # Tool diagnostics remain local; no credentials are passed as arguments.
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Signing tool failed")
    return result


def identity() -> str:
    result = command(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"])
    matches = re.findall(r'\b([A-F0-9]{40})\s+"(Developer ID Application:[^"]+)"', result.stdout)
    requested = os.environ.get("DBOND_SIGN_IDENTITY")
    if requested:
        matches = [item for item in matches if requested in item]
    if len(matches) != 1:
        raise RuntimeError("Select one valid Developer ID Application identity using DBOND_SIGN_IDENTITY")
    return matches[0][0]


def app_path(value: Path) -> Path:
    app = value.expanduser().resolve(strict=True)
    if app.suffix != ".app" or not (app / "Contents/Info.plist").is_file():
        raise ValueError("Expected an existing macOS .app bundle")
    return app


def macho_files(app: Path) -> list[Path]:
    result = []
    for path in app.rglob("*"):
        if path.is_symlink():
            if not path.resolve().is_relative_to(app):
                raise ValueError("App contains a symlink outside the signed bundle")
        elif path.is_file():
            with path.open("rb") as stream:
                if stream.read(4) in MAGIC:
                    result.append(path)
    return sorted(result, key=lambda path: (-len(path.parts), str(path)))


def fingerprint(app: Path) -> str:
    result = hashlib.sha256()
    for path in sorted(app.rglob("*")):
        if path.is_symlink():
            data = os.readlink(path).encode()
            kind = b"link"
        elif path.is_file():
            value = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            data, kind = value.digest(), b"file"
        else:
            continue
        result.update(kind + b"\0" + str(path.relative_to(app)).encode() + b"\0" + data + b"\0")
    return result.hexdigest()


def signature(path: Path, developer: bool, team: str | None = None) -> str | None:
    command(["/usr/bin/codesign", "--verify", "--strict", path])
    if not developer:
        return None
    # Universal binaries must satisfy distribution requirements on every slice.
    binary = path
    if path.is_dir():
        metadata = plistlib.loads((path / "Contents/Info.plist").read_bytes())
        binary = path / "Contents/MacOS" / metadata["CFBundleExecutable"]
    architectures = command(["/usr/bin/lipo", "-archs", binary]).stdout.split()
    current = team
    for architecture in architectures:
        current = signature_slice(path, architecture, current)
    return current


def signature_slice(path: Path, architecture: str, team: str | None) -> str:
    info = command(["/usr/bin/codesign", "-d", "--verbose=4", "--arch", architecture, path]).stderr
    if "Authority=Developer ID Application:" not in info or "runtime" not in info or "Timestamp=" not in info:
        raise RuntimeError("A nested executable lacks Developer ID, hardened runtime, or secure timestamp")
    match = re.search(r"^TeamIdentifier=(\S+)$", info, re.MULTILINE)
    current = match.group(1) if match else None
    if current is None or (team is not None and current != team):
        raise RuntimeError("Nested executable signing team does not match the app")
    entitlements = command(["/usr/bin/codesign", "-d", "--arch", architecture,
                            "--entitlements", ":-", path], allow_failure=True)
    text = entitlements.stdout
    if text.strip():
        try:
            values = plistlib.loads(text.encode())
        except Exception as exc:
            raise RuntimeError("Could not validate signed executable entitlements") from exc
        if values.get("com.apple.security.get-task-allow"):
            raise RuntimeError("Release executable has a debug get-task-allow entitlement")
    return current


def verify_archive(archive: Path, developer: bool, team: str | None, depth: int = 0) -> int:
    """Inspect archived executables without trusting ZIP extraction paths."""
    if depth > 4:
        raise ValueError("Embedded archive nesting is too deep")
    count = 0
    with tempfile.TemporaryDirectory(prefix="dbond-archive-signature-") as temp:
        with zipfile.ZipFile(archive) as source:
            names = set()
            for entry in source.infolist():
                name = Path(entry.filename)
                if name.is_absolute() or ".." in name.parts or entry.filename in names:
                    raise ValueError("Unsafe or duplicate embedded archive path")
                names.add(entry.filename)
                if entry.is_dir():
                    continue
                if entry.file_size > 256 * 1024 * 1024:
                    raise ValueError("Embedded archive member is too large")
                with source.open(entry) as stream:
                    header = stream.read(4)
                    nested = name.suffix.lower() in (".mcpb", ".zip")
                    if header not in MAGIC and not nested:
                        continue
                    target = Path(temp) / str(len(names))
                    with target.open("wb") as output:
                        output.write(header)
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            output.write(block)
                if nested:
                    count += verify_archive(target, developer, team, depth + 1)
                else:
                    signature(target, developer, team)
                    count += 1
    return count


def verify(app: Path, mode: str, standalone_mcpb: Path | None = None) -> dict:
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", app])
    developer = mode == "developer-id"
    team = signature(app, developer)
    binaries = macho_files(app)
    for binary in binaries:
        signature(binary, developer, team)
    archived = sum(verify_archive(path, developer, team) for path in app.rglob("*")
                   if path.is_file() and path.suffix.lower() in (".mcpb", ".zip"))
    if standalone_mcpb is not None:
        embedded = app / "Contents/Resources/agent-installers/d-bond-claude.mcpb"
        if hashlib.sha256(embedded.read_bytes()).digest() != hashlib.sha256(standalone_mcpb.read_bytes()).digest():
            raise RuntimeError("Standalone MCPB differs from the signed archive embedded in the app")
    return {"mode": mode, "nestedMachO": len(binaries), "archivedMachO": archived, "verified": True,
            "bundleDigest": fingerprint(app)}


def sign(app: Path, mode: str) -> dict:
    selected = identity() if mode == "developer-id" else "-"
    flags = ["--force", "--sign", selected]
    if mode == "developer-id":
        flags += ["--options", "runtime", "--timestamp"]
    for binary in macho_files(app):
        command(["/usr/bin/codesign"] + flags + [binary])
    bundles = [path for path in app.rglob("*") if path.is_dir() and not path.is_symlink()
               and path.suffix in (".app", ".framework", ".xpc", ".appex", ".bundle")
               and ((path / "Contents/Info.plist").is_file() or (path / "Info.plist").is_file()
                    or path.suffix == ".framework")]
    for bundle in sorted(bundles, key=lambda path: -len(path.parts)):
        command(["/usr/bin/codesign"] + flags + [bundle])
    command(["/usr/bin/codesign"] + flags + [app])
    return verify(app, mode)


def sign_binary(path: Path, mode: str) -> dict:
    """Sign compiled archive members before the containing MCPB is created."""
    selected = identity() if mode == "developer-id" else "-"
    flags = ["--force", "--sign", selected]
    if mode == "developer-id":
        flags += ["--options", "runtime", "--timestamp"]
    command(["/usr/bin/codesign"] + flags + [path])
    signature(path, mode == "developer-id")
    return {"mode": mode, "verified": True}


def profile() -> str:
    value = os.environ.get("DBOND_NOTARY_PROFILE")
    if not value:
        raise RuntimeError("Set DBOND_NOTARY_PROFILE to an existing notarytool Keychain profile")
    return value


def preflight() -> dict:
    identity()
    result = command(["/usr/bin/xcrun", "notarytool", "history", "--keychain-profile", profile(),
                      "--output-format", "json"], allow_failure=True)
    if result.returncode:
        raise RuntimeError("The selected notarytool Keychain profile could not authenticate")
    return {"developerIDAvailable": True, "notaryProfileAuthenticated": True}


def notarize(app: Path, expected: str, receipts: Path) -> dict:
    details = verify(app, "developer-id")
    if details["bundleDigest"] != expected:
        raise RuntimeError("App changed since approval: exact bundle digest does not match")
    preflight()
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = receipts / (expected + ".json")
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}

    def save():
        # Private operational receipts are outside tracked source.
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        receipt_path.chmod(0o600)

    def store_log() -> dict:
        log = command(["/usr/bin/xcrun", "notarytool", "log", receipt["id"], "--keychain-profile", profile()])
        log_path = receipts / (expected + "-apple-log.json")
        log_path.write_text(log.stdout)
        log_path.chmod(0o600)
        return json.loads(log.stdout)

    if receipt.get("status") == "submission-uncertain":
        raise RuntimeError("Previous upload has an unknown outcome; reconcile Apple history before another submission")
    if not receipt.get("id"):
        receipt.update(bundleDigest=expected, status="submission-uncertain")
        save()  # Survives a disconnect immediately after Apple's upload acceptance.
        with tempfile.TemporaryDirectory(prefix="dbond-notary-") as temp:
            archive = Path(temp) / "D-Bond.zip"
            command(["/usr/bin/ditto", "-c", "-k", "--norsrc", "--noextattr", "--noqtn", "--keepParent", app, archive])
            result = command(["/usr/bin/xcrun", "notarytool", "submit", archive,
                              "--keychain-profile", profile(), "--output-format", "json", "--no-wait"])
        submitted = json.loads(result.stdout)
        if not submitted.get("id"):
            raise RuntimeError("Apple returned no submission ID; reconcile history before retrying")
        receipt.update(id=submitted["id"], status=submitted.get("status", "In Progress"))
        save()
    if receipt.get("status") != "Accepted":
        result = command(["/usr/bin/xcrun", "notarytool", "wait", receipt["id"],
                          "--keychain-profile", profile(), "--timeout", "20m", "--output-format", "json"],
                         allow_failure=True)
        try:
            receipt["status"] = json.loads(result.stdout).get("status", "In Progress")
        except ValueError:
            receipt["status"] = "In Progress"
        save()
        if receipt["status"] != "Accepted":
            if receipt["status"] != "In Progress":
                store_log()
            raise RuntimeError("Apple notarization is " + receipt["status"] + "; resume this same digest, do not resubmit")
    log = store_log()
    issues = log.get("issues") or []
    if any(issue.get("severity") == "error" for issue in issues):
        raise RuntimeError("Apple's notarization log contains errors; inspect the private saved log")
    if fingerprint(app) != expected:
        raise RuntimeError("App changed during notarization; refusing to staple a different artifact")
    command(["/usr/bin/xcrun", "stapler", "staple", app])
    command(["/usr/bin/xcrun", "stapler", "validate", app])
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", app])
    command(["/usr/sbin/spctl", "--assess", "--type", "execute", app])
    receipt.update(stapled=True, gatekeeperAccepted=True, stapledBundleDigest=fingerprint(app),
                   notaryWarnings=sum(issue.get("severity") == "warning" for issue in issues))
    save()
    return {"status": "Accepted", "stapled": True, "gatekeeperAccepted": True,
            "submissionId": receipt["id"], "bundleDigest": receipt["stapledBundleDigest"],
            "notaryWarnings": receipt["notaryWarnings"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "sign", "sign-binary", "verify", "fingerprint", "notarize"))
    parser.add_argument("--app", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--standalone-mcpb", type=Path)
    parser.add_argument("--mode", choices=("adhoc", "developer-id"), default=os.environ.get("DBOND_SIGNING_MODE", "adhoc"))
    parser.add_argument("--expected-digest")
    parser.add_argument("--receipt-dir", type=Path, default=ROOT / ".build/notarization")
    args = parser.parse_args()
    if args.mode not in ("adhoc", "developer-id"):
        parser.error("Unsupported DBOND_SIGNING_MODE")
    if args.action == "preflight":
        result = preflight()
    elif args.action == "sign-binary":
        if args.binary is None:
            parser.error("--binary is required")
        result = sign_binary(args.binary.expanduser().resolve(strict=True), args.mode)
    else:
        if args.app is None:
            parser.error("--app is required")
        app = app_path(args.app)
        if args.action == "fingerprint":
            print(fingerprint(app))
            return
        if args.action == "notarize":
            if not args.expected_digest:
                parser.error("--expected-digest is required for an exact approved artifact")
            result = notarize(app, args.expected_digest, args.receipt_dir.expanduser().resolve())
        else:
            result = sign(app, args.mode) if args.action == "sign" else verify(app, args.mode, args.standalone_mcpb)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
