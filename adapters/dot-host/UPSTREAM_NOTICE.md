# Upstream source and runtime notices

`vendor/dots_bridge/` and `vendor/pyproject.toml` are unchanged copies of
[uezo/dots-bridge](https://github.com/uezo/dots-bridge) at commit
`46010a7bd840ae673cccf432d321e53732243152`. Every vendored file's SHA-256 is recorded
in `upstream.json`; `serve` verifies those hashes before starting. D-Bond's own
compatibility additions are outside that directory. No code from the owner's
private patched DotBridge installation is copied into this connector.

The upstream MIT license is reproduced verbatim in [vendor/LICENSE](vendor/LICENSE).

For source-development provisioning, the optional CPython distribution is pinned
to python-build-standalone release `20261110`, CPython `3.13.18`. Its macOS archive
SHA-256 values are pinned in `provision`. CPython and its bundled libraries retain
the licenses/notices included in that distribution. The archive is fetched from
the [official python-build-standalone repository](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110).

Python dependencies are pinned with PyPI artifact hashes in `requirements.lock`.
Their installed distribution metadata and license files belong with the embedded
runtime. This notice does not replace their individual licenses.
