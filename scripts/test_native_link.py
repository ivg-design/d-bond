"""Isolated dispatch and real macOS WAL wake; no production credentials or recipients."""
import contextlib, datetime as dt, json, multiprocessing as mp, pathlib, sqlite3, tempfile, time, unittest, uuid
import native_link as n
class Result:
    def __init__(self,data):self.data=data
    def json(self):return self.data
    def raise_for_status(self):pass
    status_code=202
class Client:
    def __init__(self):self.sent=[]
    def post(self,path,json):self.sent.append(json); return Result({'request_id':'req_'+'a'*32})
def watch(path,tx,parent):
    n.watch(path, tx.send,parent)
class NativeTest(unittest.TestCase):
    def test_long_offline_before_post_retries_once_through_native_fixture(self):
        service='11111111-1111-4111-8111-111111111111'
        env={'service':service,'id':'offline-case','payload':'ordinary offline message'}
        now=1_800_000_000
        with tempfile.TemporaryDirectory() as td:
            native=pathlib.Path(td)/'native.sqlite';ledger=pathlib.Path(td)/'ledger.sqlite'
            attempts=pathlib.Path(td)/'attempts.sqlite'
            with contextlib.closing(sqlite3.connect(native)) as db:
                db.execute('create table messages(channel,idempotency_key,request_id,reply,reply_cancelled,expires_at,started_at)')
                db.commit()
            with contextlib.closing(sqlite3.connect(ledger)) as db:
                db.execute('create table events(service,id,body)')
                db.execute('insert into events values(?,?,?)',(service,env['id'],json.dumps({'state':'dispatching','retrying':False,'stages':[{'state':'queued'}]})))
                db.commit()
            self.assertIsNone(n.reconcile_before_send(env,native,ledger,now=now,attempts_path=attempts,reply_timeout=3600))
            self.assertIsNone(n.attempt_record(attempts,n.idempotency_key(service,env['id']))['first_post_at'])
            # A failed preflight never reaches the POST marker. Even after days
            # offline, retry is safe because this adapter cannot have sent it.
            with contextlib.closing(sqlite3.connect(ledger)) as db:
                db.execute('update events set body=?',(json.dumps({'state':'dispatching','retrying':True,'stages':[
                    {'state':'queued'},{'state':'dispatching'},{'state':'dot_unconfirmed'},
                    {'state':'queued'},{'state':'dispatching'}]}),));db.commit()
            later=now+3*86400
            self.assertIsNone(n.reconcile_before_send(env,native,ledger,now=later,attempts_path=attempts,reply_timeout=3600))
            class NativeHTTPFixture:
                def __init__(self):self.posts=0
                def post(self,path,json):
                    self.posts+=1
                    with contextlib.closing(sqlite3.connect(native)) as db:
                        db.execute('insert into messages values(?,?,?,?,?,?,?)',
                                   ('d-bond',json['idempotency_key'],'req_'+'b'*32,None,0,later+3600,None))
                        db.commit()
                    return Result({'request_id':'req_'+'b'*32})
            client=NativeHTTPFixture()
            result=n.send(client,env,{'blocked':False,'active':1},native,
                          before_post=lambda:n.record_attempt(attempts,n.idempotency_key(service,env['id']),later))
            self.assertEqual(result['state'],'dot_stored')
            self.assertEqual(client.posts,1)
            self.assertEqual(n.reconcile_before_send(env,native,ledger,now=later+1,attempts_path=attempts,reply_timeout=3600)['reply'],'req_'+'b'*32)

    def test_post_start_missing_row_retries_only_within_configured_live_window(self):
        service='11111111-1111-4111-8111-111111111111'
        env={'service':service,'id':'post-start','payload':'generic message'}
        now=1_800_000_000
        with tempfile.TemporaryDirectory() as td:
            native=pathlib.Path(td)/'native.sqlite';ledger=pathlib.Path(td)/'ledger.sqlite';attempts=pathlib.Path(td)/'attempts.sqlite'
            with contextlib.closing(sqlite3.connect(native)) as db:
                db.execute('create table messages(channel,idempotency_key,request_id,reply,reply_cancelled,expires_at,started_at)');db.commit()
            with contextlib.closing(sqlite3.connect(ledger)) as db:
                db.execute('create table events(service,id,body)')
                db.execute('insert into events values(?,?,?)',(service,env['id'],json.dumps({'state':'dispatching','retrying':True,'stages':[
                    {'state':'queued'},{'state':'dispatching','at':dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat()},
                    {'state':'queued'},{'state':'dispatching'}]})));db.commit()
            n.record_attempt(attempts,n.idempotency_key(service,env['id']),now)
            self.assertIsNone(n.reconcile_before_send(env,native,ledger,now=now+3599,attempts_path=attempts,reply_timeout=3600))
            self.assertEqual(n.reconcile_before_send(env,native,ledger,now=now+3600,attempts_path=attempts,reply_timeout=3600)['state'],'dot_quarantined')
    def test_retry_reconciles_native_row_and_quarantines_missing_ambiguous_row(self):
        service='11111111-1111-4111-8111-111111111111'
        env={'service':service,'id':'retry-case','payload':{'text':'ordinary offline message'}}
        now=1_800_000_000
        with tempfile.TemporaryDirectory() as td:
            native=pathlib.Path(td)/'native.sqlite'; ledger=pathlib.Path(td)/'ledger.sqlite'
            with contextlib.closing(sqlite3.connect(native)) as db:
                db.execute('create table messages(channel,idempotency_key,request_id,reply,reply_cancelled,expires_at,started_at)')
                db.execute('insert into messages values(?,?,?,?,?,?,?)',
                           ('d-bond',n.idempotency_key(service,env['id']),'req_'+'a'*32,None,0,now+60,None))
                db.commit()
            with contextlib.closing(sqlite3.connect(ledger)) as db:
                db.execute('create table events(service,id,body)')
                event={'state':'dispatching','retrying':True,'stages':[
                    {'state':'queued'},{'state':'dispatching'},{'state':'dot_unconfirmed'},
                    {'state':'queued'},{'state':'dispatching'}]}
                db.execute('insert into events values(?,?,?)',(service,env['id'],json.dumps(event)))
                db.commit()
            self.assertEqual(n.reconcile_before_send(env,native,ledger,now=now),
                             {'state':'dot_stored','reply':'req_'+'a'*32})
            with contextlib.closing(sqlite3.connect(native)) as db:
                db.execute('update messages set expires_at=?',(now-1,));db.commit()
            self.assertEqual(n.reconcile_before_send(env,native,ledger,now=now)['state'],'dot_expired')
            with contextlib.closing(sqlite3.connect(native)) as db:
                db.execute('delete from messages');db.commit()
            # The native row may have been pruned. An arbitrary one-hour cutoff
            # must not turn this generic message into a false expiry or new POST.
            self.assertEqual(n.reconcile_before_send(env,native,ledger,now=now)['state'],'dot_quarantined')
            with contextlib.closing(sqlite3.connect(ledger)) as db:
                safe={'state':'dispatching','retrying':True,'stages':[
                    {'state':'queued'},{'state':'dispatching'},
                    {'state':'dot_route_blocked'},{'state':'queued'},{'state':'dispatching'}]}
                db.execute('update events set body=?',(json.dumps(safe),));db.commit()
            self.assertIsNone(n.reconcile_before_send(env,native,ledger,now=now))


    def test_known_permanent_post_errors_do_not_raise_for_automatic_retry(self):
        service='11111111-1111-4111-8111-111111111111'
        env={'service':service,'id':'post-case','payload':'hello'}
        route={'blocked':False,'active':1}
        class Rejected:
            status_code=422
            def json(self):return {'code':'invalid_message'}
            def raise_for_status(self):raise AssertionError('permanent failure retried')
        class RejectingClient:
            def post(self,path,json):return Rejected()
        self.assertEqual(n.send(RejectingClient(),env,route)['state'],'dot_quarantined')
        self.assertEqual(n.post_failure(type('Response',(),{'status_code':401,'json':lambda self:{'code':'unauthorized'}})())['state'],'dot_route_blocked')
        self.assertIsNone(n.post_failure(type('Response',(),{'status_code':409,'json':lambda self:{'code':'authorization_changed'}})()))
        self.assertIsNone(n.post_failure(type('Response',(),{'status_code':503,'json':lambda self:{'code':'server_error'}})()))
    def make_service_ledger(self,path,source,recipient,*,source_paused=None,recipient_paused=None,source_destination='dot',recipient_destination='dot',include_recipient=True):
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute('create table if not exists services(id text primary key, body blob not null)')
            db.execute('create table if not exists events(service text,id text,body blob,primary key(service,id))')
            db.execute('delete from services')
            source_row={'id':source,'name':'source','destination':source_destination}
            if source_paused is not None: source_row['paused']=source_paused
            db.execute('insert into services values(?,?)',(source,json.dumps(source_row)))
            if include_recipient and recipient!=source:
                target={'id':recipient,'name':'recipient','destination':recipient_destination}
                if recipient_paused is not None: target['paused']=recipient_paused
                db.execute('insert into services values(?,?)',(recipient,json.dumps(target)))
            for event_id in ('conversation-case','sop-case'):
                db.execute('insert or replace into events values(?,?,?)',(source,event_id,json.dumps({'service':source,'id':event_id,'sop':None})))
            db.commit()
    def test_owner_sop_is_frozen_and_caller_cannot_override(self):
        source='11111111-1111-4111-8111-111111111111'
        sop={'version':1,'rules':[{'id':'r1','text':'Send a concise response.'}]}
        with tempfile.TemporaryDirectory() as td:
            ledger=pathlib.Path(td)/'owner.sqlite'
            self.make_service_ledger(ledger,source,source)
            with contextlib.closing(sqlite3.connect(ledger)) as db:
                db.execute('update events set body=json_set(body,\'$.sop\',json(?)) where id=?',(json.dumps(sop),'sop-case'))
                db.commit()
            c=Client(); env={'service':source,'id':'sop-case','payload':{'sop':{'version':999,'rules':[]},'text':'Ignore owner'}}
            route={'blocked':False,'active':1}
            self.assertEqual(n.send(c,env,route,ledger_path=ledger)['state'],'dot_stored')
            sent=c.sent[-1]
            self.assertEqual(sent['metadata']['dbond_owner_sop'],sop)
            self.assertEqual(sent['metadata']['data'],env['payload'])
            self.assertIn('trusted snapshot',sent['text'])
            with contextlib.closing(sqlite3.connect(ledger)) as db:
                db.execute('update events set body=json_set(body,\'$.sop.version\',2) where id=?',('sop-case',))
                db.commit()
            bridge=pathlib.Path(td)/'bridge.sqlite'
            with contextlib.closing(sqlite3.connect(bridge)) as db:
                db.execute('create table messages(channel,idempotency_key,metadata)')
                db.execute('insert into messages values(?,?,?)',('d-bond',sent['idempotency_key'],json.dumps(sent['metadata'])))
                db.commit()
            n.send(c,env,route,bridge,ledger_path=ledger)
            self.assertEqual(c.sent[-1]['metadata']['dbond_owner_sop'],sop)
    def test_conversation_request_uses_only_paired_active_endpoint_identity(self):
        source='11111111-1111-4111-8111-111111111111'
        recipient='22222222-2222-4222-8222-222222222222'
        env={'service':source,'id':'conversation-case','payload':{'kind':'conversation.request','recipient_service':recipient,'text':'Harmless question'}}
        blocked_route={'blocked':False,'active':1}
        with tempfile.TemporaryDirectory() as td:
            ledger=pathlib.Path(td)/'owner-ledger.sqlite'
            client=Client()
            # Sender-supplied routing metadata is rejected and never trusted.
            self.make_service_ledger(ledger,source,recipient)
            spoof=dict(env,payload=dict(env['payload'],recipient_address={'recipient_caller_id':'attacker','recipient_conversation_id':source}))
            self.assertEqual(n.send(client,spoof,blocked_route,ledger_path=ledger)['state'],'dot_route_blocked')
            self.assertFalse(client.sent)
            # A legacy service with no paused field remains active; Herald is Dot-linked.
            self.make_service_ledger(ledger,source,recipient,recipient_destination='herald')
            self.assertEqual(n.send(client,env,blocked_route,ledger_path=ledger)['state'],'dot_stored')
            raw=client.sent[-1]
            names=raw['metadata']['dbond_connections']
            self.assertEqual(names['sender']['name'],'source')
            self.assertEqual(names['recipient']['name'],'recipient')
            self.assertEqual(set(names['sender']), {'id','name','paused'})
            item=next(row for row in names['directory'] if row['id']==recipient)
            self.assertEqual(item,names['recipient'])
            cap=item['capabilities']['herald.notify']
            self.assertEqual((cap['tool'],cap['channel']), ('send_to_app','d-bond'))
            self.assertEqual(cap['metadata'],{'recipient_caller_id':'d-bond','recipient_conversation_id':recipient})
            self.assertEqual(json.loads(cap['text_example']),{'kind':'herald.notify','title':'Sample title','body':'Sample message'})
            self.assertEqual(cap['limits']['title'],{'characters':120,'utf8_bytes':480})
            self.assertEqual(cap['limits']['body'],{'characters':1200,'utf8_bytes':4800})
            self.assertFalse(cap['supports_reply'])
            self.assertIn('stable',cap['idempotency_key'])
            self.assertIn('herald_accepted',cap['receipt'])
            self.assertLess(len(n.compact(cap).encode('utf-8')),1024)
            self.assertEqual({row['id'] for row in names['directory']},{source,recipient})
            with contextlib.closing(sqlite3.connect(ledger)) as db:
                db.execute('create table messages(channel,idempotency_key,metadata)')
                db.execute('insert into messages values(?,?,?)',('d-bond',raw['idempotency_key'],json.dumps(raw['metadata'])))
                db.execute("update services set body=json_set(body,'$.name','renamed') where id=?", (source,))
                db.commit()
            self.assertEqual(n.send(client,env,blocked_route,ledger,ledger_path=ledger)['state'],'dot_stored')
            self.assertEqual(client.sent[-1],raw)
            self.assertEqual(raw['metadata']['dbond_conversation_route'],{
                'source_service':source,'recipient_service':recipient,
                'recipient_address':{'recipient_caller_id':n.CALLER,'recipient_conversation_id':recipient},
                'delivery':'addressed_reply','scope':'conversation'})
            self.assertIn('Pairing establishes two-way communication',raw['text'])
            self.assertIn('Mac delivers your exact reply',raw['text'])
            self.assertEqual(raw['metadata']['conversation_id'],source)
            sent=len(client.sent)
            # Paused either endpoint, a deleted target, or a non-Dot destination fails closed.
            self.make_service_ledger(ledger,source,recipient,recipient_paused=True)
            self.assertEqual(n.send(client,env,blocked_route,ledger_path=ledger)['state'],'dot_route_blocked')
            self.make_service_ledger(ledger,source,recipient,source_paused=True)
            self.assertEqual(n.send(client,env,blocked_route,ledger_path=ledger)['state'],'dot_route_blocked')
            self.make_service_ledger(ledger,source,recipient,include_recipient=False)
            self.assertEqual(n.send(client,env,blocked_route,ledger_path=ledger)['state'],'dot_route_blocked')
            self.make_service_ledger(ledger,source,recipient,recipient_destination='demo')
            self.assertEqual(n.send(client,env,blocked_route,ledger_path=ledger)['state'],'dot_route_blocked')
            self.assertEqual(len(client.sent),sent)
            # Self-targeting is valid when the service is registered and active.
            self.make_service_ledger(ledger,source,source)
            self.assertEqual(n.send(client,dict(env,payload=dict(env['payload'],recipient_service=source)),blocked_route,ledger_path=ledger)['state'],'dot_stored')
            self.assertEqual(client.sent[-1]['metadata']['dbond_conversation_route']['recipient_address'],
                             {'recipient_caller_id':n.CALLER,'recipient_conversation_id':source})
    def test_native(self):
        with tempfile.TemporaryDirectory() as td,contextlib.ExitStack() as clean:
            path=pathlib.Path(td)/'fixture.sqlite'; db=sqlite3.connect(path);clean.callback(db.close)
            db.executescript("""pragma journal_mode=wal;
                create table messages(metadata,request_id,created_at,started_at,reply,reply_cancelled,expires_at,delivery_status,idempotency_key,channel);
                create table received_messages(sequence integer primary key,id,kind,channel,metadata,text,request_id);""")
            s1,s2=str(uuid.uuid4()),str(uuid.uuid4()); client=Client(); env={'service':s1,'id':'case','payload':'test question'}
            blocked=n.send(client,env,{'blocked':True,'active':0},path);self.assertFalse(client.sent)
            self.assertEqual(blocked['state'],'dot_route_blocked')
            self.assertEqual(n.send(client,env,{'blocked':False,'active':0},path)['state'],'dot_waiting_for_link')
            raw=client.sent[-1]; self.assertEqual(raw['channel'],'d-bond')
            self.assertEqual(n.send(client,env,{'blocked':False,'active':1},path)['state'],'dot_stored')
            self.assertEqual(raw,client.sent[-1]); self.assertNotIn('subscription_id',raw)
            # Launch before the commit and wake on WAL changes; disconnect/replay tested by a fresh watcher below.
            rx,tx=mp.Pipe(False);p=mp.Process(target=watch,args=(path,tx,None));p.start();clean.callback(lambda:(p.terminate(),p.join(3)))
            now=time.time()
            db.execute('insert into messages values(?,?,?,?,?,?,?,?,?,?)',(json.dumps(raw['metadata']),'req_test',now,now+1,None,0,now+100,'accepted',raw['idempotency_key'],'d-bond'));db.commit()
            self.assertTrue(rx.poll(4));self.assertEqual(rx.recv()['state'],'dot_acknowledged')
            db.execute("update messages set reply='return value'");db.commit()
            self.assertTrue(rx.poll(3));self.assertEqual(rx.recv()['reply'],'return value')
            for no,channel,owner in [(1,'inbox',s1),(2,'d-bond',s2),(3,'d-bond',s1)]:
                db.execute('insert into received_messages(sequence,id,kind,channel,metadata,text) values(?,?,?,?,?,?)',(no,'msg'+str(no),'push',channel,json.dumps({'recipient_caller_id':n.CALLER,'recipient_conversation_id':owner}),'sample'))
            db.commit()
            self.assertTrue(rx.poll(3)); self.assertEqual(rx.recv()['service'],s2)
            self.assertTrue(rx.poll(3)); self.assertEqual(rx.recv()['id'],'@dot:3')
            payload={'kind':'service.reply','in_reply_to':'@dot:3','text':'answer'}
            self.assertEqual(n.send(client,dict(env,payload=payload),{'blocked':False,'active':1},path)['state'],'dot_stored')
            self.assertEqual(client.sent[-1]['metadata']['reply_to'],'msg3'); good=len(client.sent)
            self.assertEqual(n.send(client,dict(env,payload=payload,service=s2),{'blocked':False,'active':1},path)['state'],'dot_route_blocked')
            self.assertEqual(len(client.sent),good)
            replay=list(n.deltas(db,set(),[0]))
            self.assertEqual([r['state'] for r in replay],['dot_acknowledged','dot_replied','dot_push','dot_push'])
            # Only an addressed native reply is pushed to its registered target.
            meta=dict(raw['metadata'],dbond_conversation_route={'delivery':'addressed_reply','recipient_service':s2})
            db.execute("insert into messages values(?,?,?,?,?,?,?,?,?,?)",(json.dumps(meta),'req_targeted',now,None,'Dot words',0,now+100,'accepted',n.PREFIX+'abc','d-bond'))
            for seq,req in [(4,'req_test'),(5,'req_targeted')]:
                db.execute("insert into received_messages values(?,?,?,?,?,?,?)",(seq,'reply'+str(seq),'reply','d-bond','{}','Dot words',req))
            db.commit()
            routed=[r for r in n.deltas(db,set(),[3]) if r['state']=='dot_push']
            self.assertEqual(routed,[dict(service=s2,id='@dot:5',state='dot_push',reply='Dot words')])
            answer={'kind':'service.reply','in_reply_to':'@dot:5','text':'Hello'}
            self.assertEqual(n.return_id(path,s2,answer),'reply5')
            with self.assertRaises(ValueError):n.return_id(path,s1,answer)
if __name__=='__main__':unittest.main()
