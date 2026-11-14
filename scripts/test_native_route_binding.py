"""Disposable dual-host route migration, replay and event-driven observation."""
import contextlib
import datetime as dt
import io
import json
import multiprocessing as mp
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import native_link as n

SERVICE = '11111111-1111-4111-8111-111111111111'
RECIPIENT = '22222222-2222-4222-8222-222222222222'


def observe_fixture(roots, attempts, ledger, selected, tx):
    n.observe_routes(tx.send, selected_kind=selected, route_roots=roots,
                     attempts_path=attempts, ledger_path=ledger)


class RouteBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.roots = {kind: self.root/kind for kind in ('legacy', 'owned')}
        self.dbs = {}
        for kind, root in self.roots.items():
            root.mkdir()
            db = sqlite3.connect(root/'bridge.sqlite3')
            self.addCleanup(db.close)
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE messages(channel,idempotency_key,request_id,reply,reply_cancelled,expires_at,started_at,metadata,created_at,delivery_status);
                CREATE TABLE received_messages(sequence INTEGER PRIMARY KEY,id,kind,channel,metadata,text,request_id);''')
            self.dbs[kind] = db
        self.ledger = self.root/'ledger.sqlite'
        self.attempts = self.root/'attempts.sqlite'
        self.now = time.time()

    def event(self, event='fixture', *, retry=False, timed=True):
        stage = {'state': 'dispatching'}
        if timed: stage['at'] = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).isoformat()
        body = {'retrying': retry, 'state': 'dispatching', 'stages': [stage]}
        with contextlib.closing(sqlite3.connect(self.ledger)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS events(service,id,body,PRIMARY KEY(service,id))')
            db.execute('INSERT OR REPLACE INTO events VALUES (?,?,?)', (SERVICE,event,json.dumps(body)))
            db.commit()
        return {'service':SERVICE,'id':event,'payload':'Example request'}

    def reconcile(self, env, selected='owned'):
        return n.reconcile_before_send(env,self.roots[selected]/'bridge.sqlite3',self.ledger,
            now=self.now,attempts_path=self.attempts,reply_timeout=3600,
            route_kind=selected,route_roots=self.roots)

    def native_row(self, kind, env, *, reply=None, metadata=None):
        metadata = metadata or {'caller_id':n.CALLER,'conversation_id':SERVICE,'dbond_event':env['id'],
            'dbond_conversation_route':{'delivery':'addressed_reply','recipient_service':RECIPIENT}}
        self.dbs[kind].execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)',
            (n.CHANNEL,n.idempotency_key(SERVICE,env['id']),'req_'+('a' if kind=='legacy' else 'b')*32,
             reply,0,self.now+3600,None,json.dumps(metadata),self.now,'accepted'))
        self.dbs[kind].commit()

    def test_old_journal_rows_migrate_to_legacy_even_when_owned_is_selected(self):
        env = self.event()
        key = n.idempotency_key(SERVICE,env['id'])
        with contextlib.closing(sqlite3.connect(self.attempts)) as db:
            db.execute('CREATE TABLE attempts(key TEXT PRIMARY KEY,first_post_at REAL)')
            db.execute('INSERT INTO attempts VALUES (?,NULL)',(key,));db.commit()
        self.assertEqual(self.reconcile(env)['state'],'dot_route_blocked')
        row=n.attempt_record(self.attempts,key,legacy_root=self.roots['legacy'])
        self.assertTrue(n.route_matches(row,'legacy',self.roots['legacy']))
        with self.assertRaises(ValueError):
            n.record_attempt(self.attempts,key,self.now,route_kind='owned',route_root=self.roots['owned'])

    def test_committed_legacy_request_wins_after_switch_and_no_other_host_post_occurs(self):
        env = self.event(retry=True)
        self.native_row('legacy',env,reply='Late original-host answer')
        result=self.reconcile(env)
        self.assertEqual(result,{'state':'dot_replied','reply':'Late original-host answer'})
        with mock.patch.object(n,'ROOT',self.roots['owned']), mock.patch.object(n,'OWNED',True), \
             mock.patch.object(n,'LEGACY_ROOT',self.roots['legacy']), mock.patch.object(n,'OWNED_ROOT',self.roots['owned']), \
             mock.patch.object(n,'LEDGER',self.ledger), mock.patch.object(n.sys,'argv',['helper','send']), \
             mock.patch.object(n.sys,'stdin',io.StringIO(json.dumps(env))), mock.patch.object(n,'connect') as connect, \
             mock.patch.object(n,'emit') as emit:
            n.main()
        connect.assert_not_called()
        emit.assert_called_once_with(result)

    def test_unknown_prejournal_post_is_bound_legacy_and_never_replayed_into_owned(self):
        for timed in (True,False):
            env=self.event('unknown-'+str(timed),retry=True,timed=timed)
            self.assertEqual(self.reconcile(env)['state'],'dot_route_blocked')
            self.assertEqual(self.reconcile(env)['state'],'dot_route_blocked')
            row=n.attempt_record(self.attempts,n.idempotency_key(SERVICE,env['id']))
            self.assertEqual(row['route_kind'],'legacy')
            self.assertIsNotNone(row['first_post_at'])

    def test_new_owned_request_keeps_exact_root_across_reselection_and_root_change(self):
        env=self.event()
        self.assertIsNone(self.reconcile(env))
        key=n.idempotency_key(SERVICE,env['id'])
        n.record_attempt(self.attempts,key,self.now,route_kind='owned',route_root=self.roots['owned'])
        for _ in range(3):
            self.assertEqual(self.reconcile(env,'legacy')['state'],'dot_route_blocked')
            self.assertIsNone(self.reconcile(env,'owned'))
        with self.assertRaises(ValueError):
            n.record_attempt(self.attempts,key,self.now,route_kind='owned',route_root=self.root/'replacement')
        row=n.attempt_record(self.attempts,key)
        self.assertTrue(n.route_matches(row,'owned',self.roots['owned']))

    def test_native_metadata_corruption_cannot_forge_the_original_request(self):
        env=self.event()
        self.native_row('legacy',env,metadata={'caller_id':n.CALLER,'conversation_id':RECIPIENT,'dbond_event':env['id']})
        self.assertEqual(self.reconcile(env)['state'],'dot_quarantined')

    def test_migration_uses_original_timestamp_so_prune_does_not_reset_retry_window(self):
        env=self.event(retry=True)
        self.native_row('legacy',env)
        self.dbs['legacy'].execute('UPDATE messages SET created_at=?,expires_at=?',(self.now-7200,self.now-3600))
        self.dbs['legacy'].commit()
        self.assertEqual(self.reconcile(env)['state'],'dot_expired')
        record=n.attempt_record(self.attempts,n.idempotency_key(SERVICE,env['id']))
        self.assertEqual(record['first_post_at'],self.now-7200)
        self.dbs['legacy'].execute('DELETE FROM messages');self.dbs['legacy'].commit()
        self.assertEqual(self.reconcile(env,'legacy')['state'],'dot_quarantined')

    def test_before_post_transaction_rejects_a_competing_route_claim(self):
        env=self.event()
        key=n.idempotency_key(SERVICE,env['id'])
        n.record_attempt(self.attempts,key,None,route_kind='legacy',route_root=self.roots['legacy'])
        client=mock.Mock()
        with self.assertRaises(ValueError):
            n.send(client,env,{'active':1,'blocked':False},self.roots['owned']/'bridge.sqlite3',owned=True,
                   before_post=lambda:n.record_attempt(self.attempts,key,self.now,route_kind='owned',route_root=self.roots['owned']))
        client.post.assert_not_called()

    def test_observer_keeps_late_unselected_receipts_but_not_unrelated_pushes(self):
        old=self.event('old')
        fresh=self.event('fresh')
        self.native_row('legacy',old)
        self.native_row('owned',fresh)
        n.record_attempt(self.attempts,n.idempotency_key(SERVICE,'fresh'),self.now,
                         route_kind='owned',route_root=self.roots['owned'])
        for kind in self.roots:
            self.dbs[kind].execute('INSERT INTO received_messages VALUES (?,?,?,?,?,?,?)',
                (1,'push-'+kind,'push',n.CHANNEL,json.dumps({'recipient_caller_id':n.CALLER,'recipient_conversation_id':SERVICE}),kind+' push',None))
            self.dbs[kind].commit()
        rx,tx=mp.Pipe(False)
        process=mp.Process(target=observe_fixture,args=(self.roots,self.attempts,self.ledger,'owned',tx))
        process.start()
        try:
            self.assertTrue(rx.poll(5))
            first=rx.recv()
            self.assertEqual(first['id'],'@dot:'+str(n.OWNED_OFFSET+1))
            self.assertEqual(first['reply'],'owned push')
            self.assertFalse(rx.poll(.15),'Unselected unsolicited push leaked')
            self.dbs['legacy'].execute('UPDATE messages SET started_at=?,reply=?',(self.now+1,'Late legacy answer'))
            self.dbs['legacy'].execute('INSERT INTO received_messages VALUES (?,?,?,?,?,?,?)',
                (2,'reply-legacy','reply',n.CHANNEL,'{}','Late addressed answer','req_'+'a'*32))
            self.dbs['legacy'].commit()
            rows=[]
            until=time.monotonic()+5
            while len(rows)<3 and time.monotonic()<until:
                if rx.poll(.2):rows.append(rx.recv())
            self.assertEqual({r['state'] for r in rows},{'dot_acknowledged','dot_replied','dot_push'})
            reply=next(r for r in rows if r['state']=='dot_push')
            self.assertEqual((reply['service'],reply['id']),(RECIPIENT,'@dot:2'))
            for kind, source in [('legacy','@dot:2'),('owned','@dot:'+str(n.OWNED_OFFSET+1))]:
                service=RECIPIENT if kind=='legacy' else SERVICE
                payload={'kind':'service.reply','in_reply_to':source,'text':'Answer'}
                self.assertIsNotNone(n.return_id(self.roots[kind]/'bridge.sqlite3',service,payload,owned=kind=='owned'))
                with self.assertRaises(ValueError):
                    n.return_id(self.roots[kind]/'bridge.sqlite3',service,payload,owned=kind!='owned')
                with self.assertRaises(ValueError):
                    n.return_id(self.roots[kind]/'bridge.sqlite3',RECIPIENT if service==SERVICE else SERVICE,payload,owned=kind=='owned')
        finally:
            process.terminate();process.join(3)

    def test_missing_store_blocks_on_filesystem_event_without_scan_timer(self):
        class EndWait(BaseException):pass
        waits=[]
        class Queue:
            def control(self,changes,maxevents,timeout):
                if changes is None:
                    waits.append(timeout)
                    raise EndWait()
                return []
            def close(self):pass
        with mock.patch.object(n.select,'kqueue',return_value=Queue()), mock.patch.object(n.time,'sleep') as sleep:
            with self.assertRaises(EndWait):
                n.watch(self.root/'missing'/'bridge.sqlite3',lambda _:None)
        self.assertEqual(waits,[None])
        sleep.assert_not_called()

    def test_unselected_owned_reply_keeps_owned_address_namespace(self):
        env=self.event('owned-origin')
        self.native_row('owned',env,reply='Late owned answer')
        n.record_attempt(self.attempts,n.idempotency_key(SERVICE,env['id']),self.now,
                         route_kind='owned',route_root=self.roots['owned'])
        self.dbs['owned'].execute('INSERT INTO received_messages VALUES (?,?,?,?,?,?,?)',
            (1,'unrelated-owned','push',n.CHANNEL,json.dumps({'recipient_caller_id':n.CALLER,'recipient_conversation_id':SERVICE}),'Not selected',None))
        self.dbs['owned'].execute('INSERT INTO received_messages VALUES (?,?,?,?,?,?,?)',
            (2,'reply-owned','reply',n.CHANNEL,'{}','Late addressed owned answer','req_'+'b'*32))
        self.dbs['owned'].commit()
        rx,tx=mp.Pipe(False)
        process=mp.Process(target=observe_fixture,args=(self.roots,self.attempts,self.ledger,'legacy',tx))
        process.start()
        try:
            rows=[]
            until=time.monotonic()+5
            while len(rows)<2 and time.monotonic()<until:
                if rx.poll(.2):rows.append(rx.recv())
            self.assertEqual({r['state'] for r in rows},{'dot_replied','dot_push'})
            push=next(r for r in rows if r['state']=='dot_push')
            self.assertEqual(push['id'],'@dot:'+str(n.OWNED_OFFSET+2))
            self.assertEqual(push['service'],RECIPIENT)
            self.assertFalse(rx.poll(.15),'Unselected owned unsolicited push leaked')
        finally:
            process.terminate();process.join(3)


if __name__=='__main__':unittest.main()
