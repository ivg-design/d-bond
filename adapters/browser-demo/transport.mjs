const KEY = 'dbondBrowserDemo';

export function validatePairing(endpoint, secret) {
  const url = new URL(endpoint);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password ||
      url.search || url.hash || !/^\/v1\/services\/[0-9a-f-]{36}\/events$/.test(url.pathname) ||
      !Number(url.port || 80) || Number(url.port || 80) > 65535 ||
      typeof secret !== 'string' || !/^[A-Za-z0-9+/]+={0,2}$/.test(secret)) {
    throw new Error('Use the loopback service endpoint and one-time credential from D-Bond.');
  }
  return url.href;
}

export class Transport {
  constructor(storage, fetcher = (...args) => fetch(...args)) { this.storage = storage; this.fetcher = fetcher; this.serial = Promise.resolve(); }
  mutate(fn) {
    const result = this.serial.then(async () => { const state = await this.state(); await fn(state); return this.save(state); });
    this.serial = result.catch(() => {});
    return result;
  }
  async state() {
    return (await this.storage.get(KEY))[KEY] || {endpoint: '', secret: '', cursor: 0, pending: [], receipts: []};
  }
  async save(state) { await this.storage.set({[KEY]: state}); return this.publicState(state); }
  publicState(state) {
    return {endpoint: state.endpoint, connected: !!state.secret, cursor: state.cursor,
      pending: state.pending, receipts: state.receipts};
  }
  async configure(endpoint, secret) {
    endpoint = validatePairing(endpoint, secret);
    return this.mutate(state => Object.assign(state, {endpoint, secret, cursor: 0, pending: [], receipts: []}));
  }
  async clear() {
    const result = this.serial.then(async () => { await this.storage.remove(KEY); return this.publicState(await this.state()); });
    this.serial = result.catch(() => {});
    return result;
  }
  async enqueue(payload) {
    const state = await this.state();
    if (!state.secret) throw new Error('Pair this trial with D-Bond first.');
    if (state.pending.length >= 20) throw new Error('Trial outbox is full.');
    const id = crypto.randomUUID();
    await this.mutate(current => current.pending.push({id, payload, queuedAt: new Date().toISOString()}));
    return this.flush();
  }
  async request(state, method, url, body) {
    const response = await this.fetcher(url, {method, headers: {
      Authorization: `Bearer ${state.secret}`,
      ...(body === undefined ? {} : {'Content-Type': 'application/json'})
    }, ...(body === undefined ? {} : {body: JSON.stringify(body)}), cache: 'no-store'});
    const value = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(`D-Bond HTTP ${response.status}: ${value.error || response.statusText}`);
    return value;
  }
  async flush() {
    const state = await this.state();
    if (!state.secret) throw new Error('Pair this trial with D-Bond first.');
    for (const item of [...state.pending]) {
      await this.request(state, 'POST', state.endpoint, {id: item.id, payload: item.payload});
      await this.mutate(latest => { latest.pending = latest.pending.filter(row => row.id !== item.id); });
    }
    return this.publicState(await this.state());
  }
  async receive(onReceipt = () => {}, signal) {
    const state = await this.state();
    if (!state.secret) throw new Error('Pair this trial with D-Bond first.');
    const response = await this.fetcher(`${state.endpoint}/stream?after=${state.cursor}`, {
      method: 'GET', headers: {Authorization: `Bearer ${state.secret}`, Accept: 'text/event-stream'},
      cache: 'no-store', signal
    });
    if (!response.ok) throw new Error(`D-Bond stream HTTP ${response.status}`);
    if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) {
      throw new Error('D-Bond did not return an event stream.');
    }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true}).replaceAll('\r\n', '\n');
        if (buffer.length > 65536) throw new Error('D-Bond stream frame is too large.');
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
          const lines = frame.split('\n');
          const idLine = lines.find(line => line.startsWith('id:'));
          const dataLine = lines.find(line => line.startsWith('data:'));
          if (!idLine || !dataLine) continue; // Comments and heartbeat frames.
          const row = JSON.parse(dataLine.slice(5).trimStart());
          if (!Number.isSafeInteger(row.sequence) || String(row.sequence) !== idLine.slice(3).trim() ||
              typeof row.id !== 'string' || typeof row.state !== 'string' || typeof row.reply !== 'string') {
            throw new Error('Invalid D-Bond stream frame.');
          }
          const saved = await this.mutate(current => {
            if (row.sequence <= current.cursor) return;
            current.receipts.push({id: row.id, sequence: row.sequence, state: row.state, reply: row.reply});
            current.receipts = current.receipts.slice(-100);
            current.cursor = row.sequence;
          });
          onReceipt(saved); // Receipt and cursor are durable before the UI hears about it.
        }
      }
    } finally { reader.releaseLock(); }
    return this.publicState(await this.state());
  }
}
