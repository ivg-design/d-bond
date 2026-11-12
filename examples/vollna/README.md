# Forward a job from an existing webhook receiver

Create or reuse **Vollna / Job intake** in D-Bond. Set its SOP under **Connections → Rules**. If you used the GUI wizard, save the pairing from its confirmation screen with `dbond link vollna-intake`; the command prompts privately. A previous `dbond enroll vollna-intake` already did this. Keep inbound webhook authentication and the existing durable job ledger in your receiver.

From this checkout, test a synthetic payload without delivering anything:

```sh
echo '{"title":"Example design job","questions":["When could you start?"]}' |
  python3 examples/vollna/forward_job.py --job-id example --revision 1 --preview
```

At your watcher's existing send boundary, run the same command **without** `--preview`, with the persisted job and the source's real stable ID and revision. A retry must use the same ID, revision and payload. A changed job is a new revision. Do not start a second sender beside an active forwarder. D-Bond attaches its own copy of the current SOP on receipt; no SOP, timestamp, or bearer belongs in the incoming job JSON. A producer-supplied source timestamp is fine as job data.

A successful response confirms local storage. Open **Connections → Events** to distinguish it from a Dot acknowledgement and reply. This example does not keep a source ledger or enforce a proposal SOP; the operator's receiver and draft/notification adapters own those responsibilities.
