# Install and connect D-Bond

On a Mac with Swift 5.9+ and macOS 13 or later, run the following from the source checkout:

```sh
bash scripts/install_app.sh
open "$HOME/Applications/D-Bond.app"
```

The app runs in the menu bar. Opening it again brings up **Connections**. Choose **Start at Login** in the menu if you want it to run after sign-in. A fresh installation can test **Local demo echo** immediately. To reach your actual Dot you also need your own compatible Dot Bridge Transport and its single native `d-bond` subscription. Installing the Mac app alone doesn't create that account connection.

## Connect a service

2. Select the destination. For a browser extension that calls the broker directly, enter the exact extension ID from Chrome. Copy the endpoint and one-time secret into that service’s local settings. Existing connections have a **Setup** button with instructions. A pairing is saved even when the service is offline; in the window, “connected” means a receiver is listening.
3. Your service posts a stable event ID and a JSON payload to its loopback endpoint. For replies, it opens the authenticated event stream. Its integration decides what to do when a reply arrives. A public webhook uses an authenticated receiver in front of this loopback connection.


## Attach your SOP once

In **Connections**, choose **Rules** beside the service, add a rule, paste the instructions and save. Rules can hold multi-paragraph documents. Order them as Dot should read them. The owner can change a connection’s rules without re-pairing, editing the webhook body, or changing another service. Each accepted service request carries the rule version it had at intake; retries of the same event retain it.



## Inspect a delivery


- A **queued** timestamp means the local Mac stored the event. **dot_stored** means the transport accepted it into storage.
- **dot_acknowledged** and **dot_replied** are separate. The UTC times with milliseconds show when D-Bond observed those changes; they do not establish when a person saw a banner or a second app acted.
- An empty stage is unknown, not zero elapsed time. Older events may have no timestamp history. Reconcile an uncertain send using the original event ID.
