import {Transport} from './transport.mjs';

chrome.storage.local.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'});
const transport = new Transport(chrome.storage.local);
let serial = Promise.resolve();
let activeStream = null;
function runExclusive(fn) {
  const result = serial.then(fn);
  serial = result.catch(() => {});
  return result;
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || !message || typeof message !== 'object') return;
  const actions = {
    state: () => transport.state().then(s => transport.publicState(s)),
    configure: () => { activeStream?.abort(); return transport.configure(message.endpoint, message.secret); },
    clear: () => { activeStream?.abort(); return transport.clear(); },
    send: () => transport.enqueue(message.payload),
    retry: () => transport.flush(),
    receive: () => {
      if (activeStream) throw new Error('A stream is already open.');
      activeStream = new AbortController();
      return transport.receive(state => chrome.runtime.sendMessage({type: 'dbondReceipt', state}).catch(() => {}), activeStream.signal)
        .finally(() => { activeStream = null; });
    }
  };
  if (!Object.hasOwn(actions, message.type)) return;
  const job = message.type === 'receive' ? Promise.resolve().then(actions.receive) : runExclusive(actions[message.type]);
  job.then(state => respond({ok: true, state}),
    error => respond({ok: false, error: String(error.message || error)}));
  return true;
});
