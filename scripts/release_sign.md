# Release signing and notarization

Local/source builds default to ad hoc signing. A public Mac release can opt into
Developer ID signing without putting an identity, account, private key, password,
or Keychain profile name in source.

```sh
DBOND_SIGNING_MODE=developer-id bash scripts/bundle.sh
```

The signer selects the sole valid Developer ID Application identity in the local
Keychain. When there are multiple identities, set `DBOND_SIGN_IDENTITY` to the
chosen certificate's name or SHA-1 fingerprint. Every nested Mach-O file,
including Python native extensions, is signed and verified before the outer app.
Developer ID signing enables hardened runtime and requests Apple's secure
timestamp. No debug entitlements or library-validation exceptions are added.
The bundle process runs the embedded runtime smoke check again after signing.

The Claude MCPB builder signs its universal native launcher before creating the
archive, using the same signing mode. The standalone MCPB and the archive embedded
in the app are copied from that one signed artifact. Release verification also
opens embedded MCPB/ZIP archives and verifies every Mach-O slice against the
outer app's signing team, hardened-runtime setting, and secure timestamp.
`verify --standalone-mcpb PATH` additionally requires byte-identical standalone
and embedded archives. Signing the app cannot repair unsigned code inside an
already-created archive; rebuild the MCPB with the intended signing mode first.

Authenticate an existing notarytool Keychain profile without uploading anything:

```sh
DBOND_NOTARY_PROFILE=your-stored-profile python3 scripts/release_sign.py preflight
```

To build and notarize the reviewed Git revision for an authorized public release:

```sh
DBOND_SIGNING_MODE=developer-id DBOND_NOTARIZE=1 \
DBOND_NOTARY_PROFILE=your-stored-profile bash scripts/package_release.sh HEAD
```

`DBOND_NOTARIZE=1` explicitly enables upload to Apple's notary service. It requires
Developer ID mode and an authenticated profile; it never silently falls back to
ad hoc signing. The default remains `DBOND_NOTARIZE=0`. The release command
packages the exact selected Git revision, signs it, computes its bundle digest,
submits that finished app, staples the accepted ticket, and verifies Gatekeeper
acceptance. It then archives the stapled app and repeats signature, ticket, and
Gatekeeper checks on the extracted ZIP. GitHub publication is separate.

An already completed app can be handled directly:

```sh
python3 scripts/release_sign.py sign --mode developer-id --app dist/D-Bond.app
python3 scripts/release_sign.py fingerprint --app dist/D-Bond.app
DBOND_NOTARY_PROFILE=your-stored-profile python3 scripts/release_sign.py notarize \
  --app dist/D-Bond.app --expected-digest THE_REVIEWED_BUNDLE_DIGEST
```

Submission requires that exact digest and checks it again before stapling.
Private local submission receipts and Apple's log stay under ignored
`.build/notarization/`, or the explicit `--receipt-dir`. A returned submission ID
is recorded before waiting. Interrupted waits resume the same submission when
the command is repeated for the same unchanged app; they do not re-upload it.
An upload with an unknown outcome stops for history reconciliation before any
retry. Once stapled, the bundle has a new digest; do not re-sign it or mutate it
before distribution.

Apple's requirements and command-line workflow:
[Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution),
[Customizing the notarization workflow](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow).
