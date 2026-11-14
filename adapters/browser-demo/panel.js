const $ = id => document.getElementById(id);
let open = true;
async function call(type, extra = {}) {
  const result = await chrome.runtime.sendMessage({type, ...extra});
  if (!result?.ok) throw new Error(result?.error || 'Service worker unavailable');
  show(result.state);
  return result.state;
}
function show(state) {
  $('endpoint').value = state.endpoint || $('endpoint').value;
  $('pending').textContent = JSON.stringify(state.pending, null, 2);
  $('receipts').textContent = JSON.stringify([...state.receipts].reverse(), null, 2);
  $('send').disabled = !state.connected;
  $('retry').disabled = !state.connected || !state.pending.length;
}
function status(value) { $('status').textContent = value; }
async function action(fn) { try { await fn(); status('Ready'); } catch(e) { status(e.message); } }
$('pair').onclick = () => action(async () => { await call('configure', {endpoint: $('endpoint').value.trim(), secret: $('secret').value.trim()}); $('secret').value = ''; });
$('clear').onclick = () => action(async () => { await call('clear'); $('endpoint').value = ''; $('secret').value = ''; });
$('send').onclick = () => action(async () => { const payload = JSON.parse($('payload').value); await call('send', {payload}); });
$('retry').onclick = () => action(() => call('retry'));
window.addEventListener('pagehide', () => { open = false; });
chrome.runtime.onMessage.addListener(message => {
  if (message?.type === 'dbondReceipt') show(message.state);
});
await action(() => call('state'));
// The visible panel rearms only after the push stream disconnects.
while (open) {
  try { const state = await call('state'); if (state.connected) await call('receive');
    else await new Promise(resolve => setTimeout(resolve, 1000));
  } catch(e) { status(e.message); await new Promise(resolve => setTimeout(resolve, 3000)); }
}
