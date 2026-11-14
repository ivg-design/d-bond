#!/usr/bin/env python3
"""Build an offline, relocatable Apple silicon Python payload for D-Bond.

This is a release-builder tool. Nothing is installed into the caller's Python or
the user's application data. Downloaded archives and wheels are hash-verified.
The shipped runtime never downloads dependencies or invokes pip.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "scripts/dot-runtime/runtime.lock.json"
REQUIREMENTS = ROOT / "adapters/dot-host/requirements.lock"
PAYLOAD_FORMAT = 1
MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe",
               b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def clean_env() -> dict[str, str]:
    # Never allow builder pip config, PYTHONPATH or credentials to affect payloads.
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home()),
           "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("TMPDIR", "SYSTEMROOT"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def run(command: list[str | Path], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(value) for value in command], check=True,
                          env=clean_env(), **kwargs)


def fetch(artifact: dict, directory: Path) -> Path:
    path = directory / artifact["filename"]
    if not path.exists():
        temp = path.with_suffix(path.suffix + ".partial")
        request = urllib.request.Request(artifact["url"], headers={"User-Agent": "D-Bond-runtime-builder/1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temp.open("wb") as output:
                shutil.copyfileobj(response, output)
            if digest(temp) != artifact["sha256"]:
                raise ValueError("Downloaded archive SHA-256 mismatch: " + artifact["filename"])
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    if path.is_symlink() or digest(path) != artifact["sha256"]:
        raise ValueError("Cached archive SHA-256 mismatch: " + artifact["filename"])
    return path


def extract_runtime(archive: Path, output: Path) -> None:
    # Validate every archive path and link before extracting with macOS tar.
    with tarfile.open(archive) as source:
        for entry in source:
            path = Path(entry.name)
            if path.is_absolute() or ".." in path.parts or path.parts[0] != "python":
                raise ValueError("Unsafe runtime archive path")
            if not (entry.isfile() or entry.isdir() or entry.issym()):
                raise ValueError("Unsupported runtime archive entry")
            if entry.issym():
                target = (output / entry.name).parent / entry.linkname
                if not target.resolve().is_relative_to(output.resolve()):
                    raise ValueError("Unsafe runtime archive symlink")
    run(["/usr/bin/tar", "-xzf", archive, "-C", output])


def python(payload: Path) -> Path:
    return payload / "python/bin/python3.13"


def notice_files(runtime_lock: dict, output: Path) -> None:
    # The install-only archive omits dependency notices; the matching full archive
    # supplies them. Audited verbatim copies are checked into source so builders
    # need no external zstd executable to unpack the full .tar.zst archive.
    notices = output / "notices/python-build-standalone"
    notices.mkdir(parents=True)
    for name, expected in runtime_lock["noticeFiles"].items():
        source = LOCK.parent / name
        if not source.resolve().is_relative_to(LOCK.parent.resolve()) or source.is_symlink():
            raise ValueError("Unsafe license notice path")
        if digest(source) != expected:
            raise ValueError("Python license notice checksum mismatch: " + name)
        shutil.copyfile(source, notices / source.name)
    if not (notices / "LICENSE.cpython.txt").is_file():
        raise ValueError("Upstream Python license notices are missing")


def inventory(payload: Path) -> dict[str, str]:
    files = {}
    for path in sorted(payload.rglob("*")):
        if path.is_symlink():
            target = os.readlink(path)
            if not path.resolve().is_relative_to(payload.resolve()):
                raise ValueError("Payload symlink escapes runtime")
            files[str(path.relative_to(payload))] = "symlink:" + target
        elif path.is_file():
            files[str(path.relative_to(payload))] = digest(path)
    return files


def smoke(payload: Path) -> dict:
    code = """
import hashlib, importlib.metadata, json, os, platform, sqlite3, ssl, sys
import anyio, cryptography, fastmcp, httpx, mcp, pydantic_core, starlette, uvicorn, websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel
key = Ed25519PrivateKey.generate()
key.public_key().verify(key.sign(b'D-Bond runtime smoke'), b'D-Bond runtime smoke')
class Check(BaseModel):
    value: int
assert Check(value='7').value == 7
db = sqlite3.connect(':memory:')
assert db.execute('select 1').fetchone() == (1,)
assert sys.version_info[:2] == (3, 13)
assert platform.machine() == 'arm64'
assert sys.flags.isolated and sys.flags.dont_write_bytecode
assert not any('/opt/homebrew/' in value or '/Library/Frameworks/' in value for value in sys.path)
ssl.create_default_context(cafile=__import__('certifi').where())
print(json.dumps({'python': platform.python_version(), 'architecture': platform.machine(),
                  'dependencies': len(list(importlib.metadata.distributions())),
                  'isolated': bool(sys.flags.isolated), 'bytecodeWrites': False}))
"""
    env = clean_env()
    env.update(PATH="/no-external-tools", PYTHONHOME="/does-not-exist", PYTHONPATH="/does-not-exist")
    with tempfile.TemporaryDirectory(prefix="dbond-runtime-smoke-") as home:
        env["HOME"] = home
        result = subprocess.run([str(python(payload)), "-I", "-B", "-c", code],
                                check=True, capture_output=True, text=True, env=env, cwd="/")
    return json.loads(result.stdout)


def sign(payload: Path, identity: str) -> None:
    # Sign every nested extension/dylib first. Never modify a signed release app
    # afterwards; its enclosing bundle is signed by bundle.sh.
    binaries = []
    for path in payload.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in MACHO_MAGIC:
                    binaries.append(path)
    for path in sorted(binaries, key=lambda value: len(value.parts), reverse=True):
        command = ["/usr/bin/codesign", "--force", "--sign", identity]
        if identity != "-":
            command += ["--options", "runtime", "--timestamp"]
        run(command + [path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        run(["/usr/bin/codesign", "--verify", "--strict", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def build(payload: Path, cache: Path, runtime_lock: dict, requirements: Path) -> None:
    archive = fetch(runtime_lock["archives"]["runtime"], cache)
    extract_runtime(archive, payload)
    wheels = cache / "wheels"
    wheels.mkdir(exist_ok=True)
    pip = [python(payload), "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check"]
    base = ["--only-binary=:all:", "--no-deps", "--require-hashes", "-r", requirements]
    run(pip + ["download", "--index-url", "https://pypi.org/simple", "--dest", wheels] + base,
        stdout=subprocess.DEVNULL)
    site = payload / "python/lib/python3.13/site-packages"
    run(pip + ["install", "--no-index", "--find-links", wheels, "--no-compile", "--target", site] + base,
        stdout=subprocess.DEVNULL)
    run(pip + ["check"], stdout=subprocess.DEVNULL)
    notice_files(runtime_lock, payload)
    # Console entry points have build-location shebangs. App launchers invoke the
    # exact interpreter/module instead; no compiler, pip, venv or PATH needed.
    shutil.rmtree(site / "bin", ignore_errors=True)
    for path in site.glob("pip*"):
        if path.is_dir():
            shutil.rmtree(path)
    shutil.rmtree(payload / "python/include", ignore_errors=True)
    shutil.rmtree(payload / "python/share", ignore_errors=True)
    for path in (payload / "python/bin").iterdir():
        if path.name not in ("python", "python3", "python3.13"):
            path.unlink()
    for path in payload.rglob("__pycache__"):
        shutil.rmtree(path)
    manifest = {"schemaVersion": 1, "payloadFormat": PAYLOAD_FORMAT,
                "runtime": runtime_lock, "requirementsSHA256": digest(requirements),
                "wheels": {path.name: digest(path) for path in sorted(wheels.glob("*.whl"))},
                "pythonRelativePath": "python/bin/python3.13"}
    (payload / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(requirements, payload / "requirements.lock")
    (payload / "THIRD_PARTY_NOTICES.txt").write_text(
        "D-Bond embedded Python runtime\n\n"
        "CPython and its bundled native dependencies: notices/python-build-standalone/.\n"
        "Third-party Python package licenses and notices are preserved in\n"
        "python/lib/python3.13/site-packages/<package>.dist-info/.\n"
        "Exact upstream archives, wheel hashes, and lock checksum: runtime-manifest.json.\n"
        "No application source, local configuration or credentials are included.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".build/dot-runtime")
    parser.add_argument("--requirements", type=Path, default=REQUIREMENTS)
    parser.add_argument("--cache-dir", type=Path,
                        default=Path(os.environ.get("DBOND_RUNTIME_CACHE", str(Path.home() / "Library/Caches/D-Bond/runtime"))))
    parser.add_argument("--sign-identity", default="-", help="nested Mach-O signing identity; default ad hoc")
    parser.add_argument("--smoke-only", action="store_true", help="verify an existing embedded runtime without changing it")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("This payload supports native Apple silicon macOS builds only")
    output = args.output.expanduser().resolve()
    if args.smoke_only:
        print(json.dumps(smoke(output), sort_keys=True))
        return
    if output.name != "dot-runtime":
        parser.error("--output must name a dedicated dot-runtime directory")
    if output.exists() and not (output / "runtime-manifest.json").is_file():
        parser.error("Refusing to replace a directory without runtime-manifest.json")
    runtime_lock = json.loads(LOCK.read_text())
    requirements = args.requirements.expanduser().resolve(strict=True)
    key = (runtime_lock["pythonVersion"] + "-" + runtime_lock["release"] + "-"
           + runtime_lock["architecture"] + "-" + digest(requirements)[:20]
           + "-" + hashlib.sha256(LOCK.read_bytes() + Path(__file__).read_bytes()).hexdigest()[:12])
    cache = args.cache_dir.expanduser().resolve() / key
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (cache / "build.lock").open("a+") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        cached_payload = cache / "payload"
        checksums = cache / "payload-checksums.json"
        if cached_payload.exists():
            if not checksums.is_file() or inventory(cached_payload) != json.loads(checksums.read_text()):
                raise ValueError("Cached runtime payload checksum mismatch; remove this cache entry and rebuild")
        else:
            with tempfile.TemporaryDirectory(prefix="staging-", dir=cache) as temp:
                staging = Path(temp)
                build(staging, cache, runtime_lock, requirements)
                smoke(staging)
                staging.rename(cached_payload)
            checksums.write_text(json.dumps(inventory(cached_payload), sort_keys=True) + "\n")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".dot-runtime-", dir=output.parent) as temp:
            staged = Path(temp) / "dot-runtime"
            shutil.copytree(cached_payload, staged, symlinks=True)
            sign(staged, args.sign_identity)
            result = smoke(staged)
            backup = Path(temp) / "previous"
            if output.exists():
                output.rename(backup)
            try:
                staged.rename(output)
            except BaseException:
                if backup.exists():
                    backup.rename(output)
                raise
        result["payloadBytes"] = sum(path.stat().st_size for path in output.rglob("*")
                                     if path.is_file() and not path.is_symlink())
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
