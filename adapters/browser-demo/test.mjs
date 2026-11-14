import assert from 'node:assert/strict';
import {Transport} from './transport.mjs';

const pair = 'http://127.0.0.1:18768/v1/services/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/events';
const secret = 'YWJjZA==';
const data = {};
const storage = {
  async get(key) { return {[key]: structuredClone(data[key])}; },
  async set(value) { Object.assign(data, structuredClone(value)); },
  async remove(key) { delete data[key]; }
};
let failFirst = true;
const posts = [];
const fetcher = async (url, options) => {
  assert.equal(options.headers.Authorization, `Bearer ${secret}`);
  if (options.method === 'POST') {
    const body = JSON.parse(options.body);
    posts.push(body);
    if (failFirst) { failFirst = false; throw new Error('temporary network loss'); }
    return {ok: true, status: 202, json: async () => ({id: body.id, state: 'queued'})};
  }
  assert.equal(url, `${pair}/stream?after=0`);
  assert.equal(options.headers.Accept, 'text/event-stream');
  const frame = `id: 1\ndata: ${JSON.stringify({id: posts[0].id, sequence: 1, state: 'local_reply', reply: 'received'})}\n\n`;
  return {ok: true, headers: {get: () => 'text/event-stream'},
    body: new ReadableStream({start(controller) {
      controller.enqueue(new TextEncoder().encode(frame.slice(0, 7)));
      controller.enqueue(new TextEncoder().encode(frame.slice(7)));
      controller.close();
    }})};
};

const a = new Transport(storage, fetcher);
await assert.rejects(a.configure('https://example.com', secret));
await a.configure(pair, secret);
await assert.rejects(a.enqueue({text: 'trial'}), /temporary network loss/);
const pending = (await a.state()).pending;
assert.equal(pending.length, 1);
const b = new Transport(storage, fetcher); // Simulate a restarted service worker.
await b.flush();
assert.deepEqual(posts[0], posts[1]);
assert.equal((await b.state()).pending.length, 0);
const observed = [];
const receipt = await b.receive(state => observed.push(state));
assert.equal(receipt.cursor, 1);
assert.equal(observed[0].cursor, 1);
assert.deepEqual(receipt.receipts[0], {id: posts[0].id, sequence: 1, state: 'local_reply', reply: 'received'});
assert.ok(!JSON.stringify(receipt).includes(secret));
await b.clear();
assert.equal((await b.state()).secret, '');
console.log('Transport retry, restart, correlation, cursor, and clear: PASS');
