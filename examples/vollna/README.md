# Optional Vollna webhook producer example

This opt-in example forwards one already persisted job version from an owner's
existing authenticated webhook receiver. It contains no webhook endpoint,
account identity, credential, private rules, proposal workflow, or installed
service configuration. It does not start a watcher or contact a source account.

The downloaded D-Bond.app bundles this example at
`Contents/Resources/agent-installers/examples/vollna/forward-job` with its
Python CLI dependency and runtime. No source checkout or separate Python install is needed. After creating an example
connection, use the app's bundled `Contents/Resources/dbond link YOUR_EXAMPLE_ALIAS`
to save its endpoint and one-time credential privately; the command prompts
without echoing the credential. Use the same alias with `forward-job`.

Create an ordinary named service in D-Bond, such as **Job source / Example**,
and explicitly save its pairing under a local alias of your choice. Rules are
optional owner-authored instructions; the source payload cannot replace them.

Validate a synthetic job locally without sending:

```sh
echo '{"title":"Example design job","questions":["When could you start?"]}' |
  python3 examples/vollna/forward_job.py --job-id example --revision 1 --preview
```

With an app at `~/Applications/D-Bond.app`, run the same preview with:

```sh
APP="$HOME/Applications/D-Bond.app"
echo '{"title":"Example design job","questions":["When could you start?"]}' |
  "$APP/Contents/Resources/agent-installers/examples/vollna/forward-job" --job-id example --revision 1 --preview
```

To opt in to one local delivery, pass `--alias YOUR_EXAMPLE_ALIAS` without
`--preview`. Run the sender at your existing receiver's dispatch boundary after
persisting the source version. Retain the receiver's own webhook authentication
and durable source cursor. A retry uses the same job ID, revision, and payload;
a changed version uses a new revision. Do not run a second producer beside an
active sender for the same source.

The example sends only to the saved loopback pairing. A stored receipt confirms
broker storage; **Connections → Events** distinguishes storage, acknowledgment,
and content replies. Downstream actions remain the receiving application's
explicit responsibility. This example never writes a draft, posts a proposal,
or sends an owner alert.
