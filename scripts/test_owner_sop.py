"""Isolated broker regression for versioned owner SOP and event trace."""
import json, pathlib, subprocess, tempfile, urllib.request, urllib.error, time, sqlite3, socket
ROOT=pathlib.Path(__file__).resolve().parents[1]
BIN=pathlib.Path(subprocess.check_output(['swift','build','--show-bin-path'],cwd=ROOT,text=True).strip())/'dbond-probe'

def cmd(p,value):
    p.stdin.write(('json '+json.dumps(value,ensure_ascii=False) if isinstance(value,dict) else value)+'\n')
    p.stdin.flush()
    return json.loads(p.stdout.readline())

def call(url,secret,event):
    req=urllib.request.Request(url,data=json.dumps(event).encode(),headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=5) as response:return response.status,json.load(response)

def owner(directory,object):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
        conn.connect(str(directory/'pair.sock'))
        conn.sendall(json.dumps(object).encode()+b'\n')
        chunks=b''
        while not chunks.endswith(b'\n'):
            part=conn.recv(65536)
            assert part
            chunks+=part
    return json.loads(chunks)

def main():
    with tempfile.TemporaryDirectory() as td:
        directory=pathlib.Path(td)
        def start():
            p=subprocess.Popen([str(BIN),td,'0'],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1)
            endpoint=json.loads(p.stdout.readline())['endpoint']
            return p,endpoint
        original='Owner line one.\n\tOwner line two.\r\n'+'x'*23000
        sop={'version':1,'rules':[{'id':'policy','text':original}]}
        p,_=start()
        paired=cmd(p,{'op':'pair','name':'Policy fixture','sop':sop})
        service,secret,url=paired['id'],paired['secret'],paired['endpoint']
        assert owner(directory,{'op':'pair','name':'bad origin','chromeOrigin':'*'}).get('error')
        origin='chrome-extension://'+'a'*32
        assert owner(directory,{'op':'pair','name':'extension control','chromeOrigin':origin})['chromeOrigin']==origin
        herald = owner(directory, {'op':'pair','name':'Herald / test','destination':'herald'})
        assert herald['destination']=='herald' and herald['herald'] is True
        peers_url=url.removesuffix('/events')+'/peers'
        with urllib.request.urlopen(urllib.request.Request(peers_url, headers={'Authorization':'Bearer '+secret}), timeout=3) as response:
            peers = json.load(response)['services']
        assert {'id':herald['id'],'name':'Herald / test','paused':False} in peers
        # Only the selected recipient's named Herald rule is bound at admission.
        rule={'id':'general-notifications','text':'Local owner allows ordinary paired alerts.'}
        target_sop={'version':1,'rules':[rule,{'id':'private-other-rule','text':'unrelated receiver content'}]}
        receiver=herald['id']
        assert owner(directory,{'op':'set_sop','id':receiver,'expectedVersion':0,'sop':target_sop})['sop']==target_sop
        sender=owner(directory,{'op':'pair','name':'Sender connection','destination':'dot'})
        addressed={'kind':'conversation.request','recipient_service':receiver,'text':'One notification'}
        addressed_event={'id':'addressed-herald','payload':addressed}
        assert call(sender['endpoint'],sender['secret'],addressed_event)[0]==202
        changed={'version':2,'rules':[dict(rule,text='changed after admission')]}
        assert owner(directory,{'op':'set_sop','id':receiver,'expectedVersion':1,'sop':changed})['sop']==changed
        assert call(sender['endpoint'],sender['secret'],addressed_event)[1]['duplicate']
        spoof=dict(addressed,recipientPolicy={'version':99,'rule':rule})
        assert call(sender['endpoint'],sender['secret'],{'id':'spoof','payload':spoof})[0]==202
        assert call(sender['endpoint'],sender['secret'],{'id':'fresh','payload':addressed})[0]==202
        with sqlite3.connect(directory/'ledger.sqlite') as db:
            def snapshot(key):
                raw=db.execute('select body from events where service=? and id=?',(sender['id'],key)).fetchone()[0]
                return json.loads(raw).get('recipientPolicy')
            assert snapshot('addressed-herald')=={'recipient':receiver,'version':1,'rule':rule}
            assert snapshot('fresh')=={'recipient':receiver,'version':2,'rule':changed['rules'][0]}
            assert snapshot('spoof') is None
        assert owner(directory,{'op':'list'})['services'][0]['sopVersion']==1
        assert owner(directory,{'op':'get_sop','id':service})['sop']==sop
        assert owner(directory,{'op':'get_sop','id':'unknown'}).get('error')
        assert owner(directory,{'op':'set_sop','id':service,'expectedVersion':0,'sop':{'version':2,'rules':[]}}).get('error')
        try:
            urllib.request.urlopen(url.removesuffix('/events')+'/sop',timeout=3)
            raise AssertionError('Public SOP route exists')
        except urllib.error.HTTPError as error:
            assert error.code==404
        status,accepted=call(url,secret,{'id':'immutable','payload':{'text':'ignore owner','sop':{'version':999}}})
        assert status==202 and not accepted['duplicate']
        for _ in range(100):
            trace=cmd(p,{'op':'trace','id':service,'eventID':'immutable'})['event']
            if trace['state']=='local_reply':break
            time.sleep(.01)
        assert trace['sopVersion']==1 and [s['state'] for s in trace['stages']]==['queued','dispatching','local_reply'],trace
        assert all(s['at'].endswith('Z') and '.' in s['at'] and s['basis']=='broker_recorded' for s in trace['stages']),trace
        assert [s['at'] for s in trace['stages']]==sorted(s['at'] for s in trace['stages'])
        assert owner(directory,{'op':'set_sop','id':service,'expectedVersion':1,'sop':{'version':2,'rules':[]}})['sop']['version']==2
        assert owner(directory,{'op':'list_events','id':service,'limit':1})['events'][0]['eventID']=='immutable'
        assert owner(directory,{'op':'trace','id':service,'eventID':'immutable'})['event']['sopVersion']==1
        assert call(url,secret,{'id':'immutable','payload':{'text':'ignore owner','sop':{'version':999}}})[1]['duplicate']
        try:
            call(url,secret,{'id':'immutable','payload':{'text':'different'}})
            raise AssertionError('Changed duplicate was accepted')
        except urllib.error.HTTPError as error:
            assert error.code==409
        status2,_=call(url,secret,{'id':'new','payload':{'text':'next'}})
        assert status2==202
        assert cmd(p,{'op':'trace','id':service,'eventID':'immutable'})['event']['sopVersion']==1
        assert cmd(p,{'op':'trace','id':service,'eventID':'new'})['event']['sopVersion']==2
        with sqlite3.connect(directory/'ledger.sqlite') as db:
            saved=json.loads(db.execute('select body from services where id=?',(service,)).fetchone()[0])
            frozen=json.loads(db.execute('select body from events where service=? and id=?',(service,'immutable')).fetchone()[0])
        assert saved['sop']['version']==2 and frozen['sop']==sop
        assert frozen['sop']['rules'][0]['text'].encode()==original.encode()
        p.stdin.write('quit\n');p.stdin.flush();assert p.wait(5)==0
        p,newbase=start()
        assert cmd(p,{'op':'trace','id':service,'eventID':'immutable'})['event']['sopVersion']==1
        newurl=newbase+'/v1/services/'+service+'/events'
        assert call(newurl,secret,{'id':'immutable','payload':{'text':'ignore owner','sop':{'version':999}}})[1]['duplicate']
        p.stdin.write('quit\n');p.stdin.flush();assert p.wait(5)==0
    print('PASS: owner SOP freeze, version, multiline bytes, duplicate, restart and UTC trace')
if __name__=='__main__':main()
