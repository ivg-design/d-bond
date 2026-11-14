# Private Dot plugin packager

This optional skill package teaches Dot to acknowledge requests and reply to the correct service. Connect the Mac app’s built-in Dot connector in ChatGPT first. In D-Bond’s Dot settings, choose **Export Dot Plugin…**, select your connected app’s binding file, and choose where to save the private ZIP. The app packages only your binding and the D-Bond skill. The same packager is available from source.

Supply your own local `.app.json` with exactly one identity pair:

```json
{"apps":{"dev-<hex-suffix>":{"id":"asdk_app_<same-hex-suffix>"}}}
```

Build a private ZIP outside this repository:

```sh
python3 adapters/dot/build_private.py --binding /path/to/your/.app.json --output /private/path/d-bond-dot.zip
```

Replace both placeholders with the identical lowercase hex suffix from your
own connected app. The ZIP contains that app identity and is for your own local installation.
Keep it private. The packager accepts only the expected app identity fields;
it never reads credentials or account data. The included skill explains
request acknowledgement, replies, owner SOP, and routing boundaries.
