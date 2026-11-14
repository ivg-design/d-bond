# Connect Vollna job alerts to Dot

When a job matches your filters, **Vollna sends an HTTPS POST to your receiver**. That receiver forwards each project to the local D-Bond pairing, which wakes your existing Dot. If you already have a webhook address, identify the app that issued it. If it belongs to a watcher you control, keep that URL in Vollna and [connect the existing watcher](#use-an-existing-watcher). A Vollna RSS or dashboard results link opens a feed or a page. For push delivery, enter the receiver's URL in Vollna. [Vollna job webhook setup](https://www.vollna.com/docs/notifications/webhooks)

## 1. Set up the Mac and Dot

1. Install **D-Bond.app** from the [release downloads](https://github.com/ivg-design/d-bond/releases/latest). Open it and turn on **Settings → Start D-Bond at login** if this Mac will run unattended.
2. In **Connectors → Configure Dot Connector…**, complete the [Dot connection steps](https://github.com/ivg-design/d-bond#connect-your-existing-dot). Have the same Dot subscribe once to `app.message.created` on channel `d-bond`, acknowledge requests and reply through the connector. Use **Check Status** and select the verified route. Every incoming job shares that subscription.
3. In **Connections → Add Connection…**, choose **Vollna job intake**, keep **Vollna / Job intake**, choose **Dot**, and click **Create Connection**. Save the displayed loopback endpoint and one-time bearer in your receiver's private settings. For the CLI sender, follow the link command below. This is the **receiver → D-Bond** connection.
4. Open **Rules** beside that pairing. Add a Markdown rule and click **Save rules**. For the first test, use a harmless instruction such as *“Reply through D-Bond with the exact job title, job ID and number of screening questions. Take no further action.”* D-Bond attaches the saved rule version when it accepts each new job. You can change the rules here after testing.

## 2. Give Vollna a receiving address

If you already have a watcher on the Mac, use the existing-watcher instructions below. For a new setup, the example [n8n workflow](n8n-job-intake.json) turns a local n8n instance into the receiver. This is an optional installation. D-Bond's **Create Vollna connection…** creates the local pairing. **Reveal bundled Vollna example** opens its folder in Finder. The workflow is also available from this repository for versions that bundle only `forward-job`.

1. Run [self-hosted n8n](https://docs.n8n.io/hosting/installation/npm/) **directly on the same Mac** as D-Bond. Its HTTP Request node must reach the loopback endpoint. In Docker, `127.0.0.1` belongs to the container. The example assumes the native installation.
2. Give this receiver a stable public HTTPS address. Use your existing HTTPS reverse proxy with a public hostname and valid certificate. Forward just the POST paths `/webhook-test/vollna-jobs` and `/webhook/vollna-jobs` to n8n on the Mac. Set n8n's `WEBHOOK_URL` to the public HTTPS origin and configure trusted proxy hops for your topology. Keep the n8n editor and D-Bond broker private. OpenAI’s MCP tunnel routes OpenAI connector calls. Vollna still needs its own reachable HTTP destination. [n8n reverse proxy setup](https://docs.n8n.io/hosting/configuration/configuration-examples/webhook-url/)
3. In n8n, import `n8n-job-intake.json` using **Import from File**. Open **Receive job notification** and create a **Basic Auth** credential for Vollna. You will enter the same username and password in Vollna. In **Store in D-Bond**, replace the entire placeholder URL with the actual endpoint from step 1. Create a separate **Header Auth** credential, with name `Authorization` and value `Bearer <your D-Bond pairing credential>`. Store these credentials in n8n's credential fields.
4. Open the webhook node, choose **Listen for test event** and copy the **Test URL**. In Vollna open **Dashboard → Notifications → Add notification → Webhook** (some versions show **Webhook → Setup**). Enter that URL, choose **Basic auth**, enter the webhook credentials and select your job filters. Choose immediate delivery where offered. The webhook feature requires a plan or add-on that includes it. [Vollna notification settings](https://www.vollna.com/docs/notifications/overview)
5. Click **Send test** in Vollna. A test can contain up to three **real recent jobs**. Keep the test-only rules until you have seen both the per-title entries and Dot's replies. For a dedicated test pairing, point the HTTP Request node at that pairing and switch both its endpoint and credential to the production pairing after validation. [Vollna’s n8n guide](https://www.vollna.com/docs/integrations/n8n)
6. Publish the n8n workflow. Paste the node's **Production URL** in the Vollna notification, then save it. Where this webhook already exists, edit that notification to keep one sender. Keep the Mac awake and set your receiver and HTTPS proxy to start after restart. Verify the next *new* matching job as described below.

The workflow reads the `projects` array in each POST and forwards one event per Upwork posting. It keeps the title, description, questions and other job fields, converts Vollna's tracked URL to a canonical job URL, and omits filter-tracking metadata. Each posting ID and content revision give it a repeatable D-Bond event ID. Replaying identical project content returns the stored receipt. Changed content produces a new revision; downstream draft and alert actions can use the job ID to find their earlier work. An unrecognized posting URL or missing title fails the execution so you can inspect the original delivery. The example configures n8n to retain failed executions. Set your own data retention and error notifications.

The webhook responds after D-Bond stores the batch. Dot reviews jobs asynchronously. Vollna's request timeout is ten seconds. For high-volume batches or outages, use a receiver with a durable queue: commit the complete batch, acknowledge it promptly, then forward each project with the same event ID on retry. If the Mac and its public route are offline, check Vollna and your receiver before assuming the job arrived. Vollna's job webhook guide does not promise an automatic redelivery schedule.

## Use an existing watcher

Keep the public address, webhook authentication and durable job ledger. At the point where your watcher dispatches an already saved job, run the bundled sender once for each project. Set it as the sole sender to D-Bond for this source. A workflow that runs on another machine needs a local dispatch component that can reach the Mac's loopback broker.

In an interactive Terminal on the Mac, link the D-Bond pairing once. Adjust the app path if you installed it in your user Applications folder.

```sh
APP="/Applications/D-Bond.app"
"$APP/Contents/Resources/dbond" link vollna-intake
```

Enter the endpoint and bearer when prompted. To verify the packaged adapter without sending a job, run:

```sh
SENDER="$APP/Contents/Resources/agent-installers/examples/vollna/forward-job"
echo '{"title":"Example design job","questions":["When could you start?"]}' |
  "$SENDER" --job-id example --revision 1 --preview
```

At the watcher's dispatch point, provide the complete project JSON on standard input:

```sh
"$SENDER" --alias vollna-intake --job-id "$JOB_ID" --revision "$SAVED_REVISION" < "$SAVED_PROJECT_JSON"
```

Use the stable marketplace job ID from the source ledger, including for two similar titles. Persist the revision with the original JSON. After an interrupted send, retry that same ID, revision and content, and mark the job handed off only on a matching stored receipt. If you run the source checkout, substitute `python3 examples/vollna/forward_job.py` for the bundled sender. This example accepts one project, up to 192 KiB including its envelope. Retain oversized jobs for handling rather than truncating them.

## 3. Verify a job and enable your rules

Open **Connections → Vollna / Job intake → Events**. Match the job title and event ID to the receiver's execution. Check the local receipt, Dot's acknowledgment, and its reply with the requested details. **View complete log** opens older entries. If nothing appears, inspect the receiver's URL, authentication, selected filters and dispatch output. If the event is stored but Dot has not answered, check the active Dot connector and subscription and the event's last stage.

After verification, save the real rules in the same **Rules** editor. Specify how Dot should qualify a job, what evidence to use for a suspected repost, where it may save a draft, how it should handle each screening question and when it may notify you. A draft store, job chat or Herald alert requires its own connected destination. To verify that workflow, check the saved draft against the job ID, read the question answers in that job's chat and confirm the alert in Herald History. A new installation starts with only the routes you configure.
