# Embedded Python runtime

`scripts/package_dot_runtime.py` builds the offline Apple silicon payload used by
D-Bond's Python connectors and helpers. Users receive the runtime inside the Mac
app; they do not install Python, a virtual environment, a compiler, or packages.

From the repository root:

```sh
python3 scripts/package_dot_runtime.py --output .build/dot-runtime
python3 scripts/package_dot_runtime.py --output .build/dot-runtime --smoke-only
```

The release bundle invokes the same helper with
`--output "$RES_DIR/dot-runtime"`. The complete directory must be embedded before
the enclosing app is signed. Its interpreter is
`Contents/Resources/dot-runtime/python/bin/python3.13`. Run helpers with that
absolute path and `-B`; third-party dependencies are already in the interpreter's
own `site-packages`. No `pip`, virtual environment, `PYTHONPATH`, or network fetch
is needed at runtime. The aliases `python` and `python3` are retained.

Use `-I -B` for isolated entry points that explicitly insert their trusted module
directories. `-I` deliberately omits the script directory, so existing scripts
that import sibling files should use `-E -s -B`, or a bootstrap that inserts the
known bundle directory. Keep mutable config, logs, bytecode, databases, and
credentials outside the signed app bundle.

The helper requires an Apple silicon Mac and builder Python 3.9 or newer. It
downloads the exact relocatable CPython build recorded in `runtime.lock.json` and
verifies its SHA-256 before extracting it. It installs only binary wheels from
the fully hashed `adapters/dot-host/requirements.lock`, checks package dependency
consistency, then removes pip, headers, and generated console scripts. It never
copies the developer's installed packages, private bridge clone, configuration,
or credentials. The selected CPython archive and native wheels target macOS 11
or later; the app's own minimum macOS version remains authoritative.

The persistent cache defaults to `~/Library/Caches/D-Bond/runtime`. Override it
with `DBOND_RUNTIME_CACHE` or `--cache-dir`. Its key contains the Python version,
upstream build, architecture, requirements checksum, and packager checksum, so a
Git revision exported into a temporary release directory reuses the same
verified payload. Concurrent builds lock each cache entry. Cached file hashes
are checked before copying; an unexpected change stops the build. A repeated
build needs no downloads while the matching cache entry is intact.

Nested Mach-O files are signed from the inside out and individually verified.
The default is ad hoc signing; `--sign-identity` accepts the same Developer ID
identity used for the outer app. Signing with a real identity enables hardened
runtime and timestamps. The helper performs a second off-PATH isolated smoke
check after signing. The release pipeline must still sign and verify the final
outer app, archive, and extracted archive. This helper does not publish,
notarize, install, or modify a running service.

`runtime-manifest.json` records the exact archive provenance, wheel checksums,
and requirements checksum without developer paths. CPython and native dependency
notices in `notices/python-build-standalone/` are unmodified files extracted from
the matching full upstream archive named in the lock. The lock pins both that
archive's checksum and each checked-in notice's checksum. Keeping these small
public text files in source avoids requiring an external zstd decoder to build
the runtime. Installed wheel `.dist-info` licenses are preserved in the payload.

To update the runtime, independently verify a new upstream release's asset
digest, replace the archive pin and matching public notices, update their hashes,
then rebuild and repeat relocation, off-PATH, dependency, nested-signature, and
outer-bundle checks. Dependency updates require a complete hashed wheel lock for
CPython 3.13 on Apple silicon; source builds are rejected.
