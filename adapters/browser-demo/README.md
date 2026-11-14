# Isolated Chrome transport trial

Load this directory as an unpacked extension in a disposable Chrome profile.
Open its side panel and paste a fresh D-Bond pairing. When creating the pairing,
allow the exact `chrome-extension://<extension-id>` origin shown by Chrome.
The panel sends stable event IDs, stores pending sends, and streams receipts
while it is open. Closing Chrome or the panel stops live receipt delivery;
reopen it to resume from its saved cursor.

`node adapters/browser-demo/test.mjs` tests transport behavior against a fake peer.
This directory is a neutral, opt-in transport example. It has no service-specific
workflow or account integration.
