"""Integration pass. Fresh Mac-local stores, no production requests or banners."""
import concurrent.futures, contextlib, json, os, pathlib, selectors, subprocess, sys, tempfile, time
import urllib.request, urllib.error, shutil, secrets, threading, hashlib, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
root = pathlib.Path(__file__).resolve().parents[1]
swift_bin = subprocess.check_output(["swift","build","--show-bin-path"],cwd=root,text=True).strip()
probe = str(pathlib.Path(swift_bin)/"dbond-probe")
venv = pathlib.Path.home()/"Library/Application Support/DotBridge/venv/bin/python"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def read_line(p):
    with selectors.DefaultSelector() as sel:
        sel.register(p.stdout, selectors.EVENT_READ)
        if not sel.select(14): raise TimeoutError("child readiness")
    line = p.stdout.readline()
    if not line: raise RuntimeError("child stopped")
    return line.strip()
def tell(p,line):
    p.stdin.write(line+"\n"); p.stdin.flush(); return json.loads(read_line(p))
def start(args):
    return subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1)
def stop(p):
    if p.poll() is None:
        p.terminate()
        try: p.wait(5)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
def call(url,key,body=None):
    raw = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url,data=raw,headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
    try:
        with opener.open(r,timeout=16) as v: return (v.status,json.load(v))
    except urllib.error.HTTPError as e: return e.code,json.load(e)
def fake_herald(folder, cleanup):
    folder.mkdir(mode=0o700)
    key = secrets.token_hex(32); requests = []
    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def do_POST(self):
            assert self.path=="/v1/notify" and self.headers.get("Authorization")=="Bearer "+key
            data=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert set(data)=={"app","title","body","id"}
            requests.append(data)
            output=json.dumps({"ok":True,"id":data["id"]}).encode()
            self.send_response(200); self.send_header("Content-Length",str(len(output))); self.end_headers(); self.wfile.write(output)
    server=ThreadingHTTPServer(("127.0.0.1",0), Endpoint)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    cleanup.callback(server.server_close);cleanup.callback(server.shutdown)
    (folder/"port").write_text(str(server.server_port));(folder/"port").chmod(0o644)
    (folder/"token").write_text(key);(folder/"token").chmod(0o600)
    return requests
def run():
    with tempfile.TemporaryDirectory() as store, contextlib.ExitStack() as cleanup:
        signed_probe = str(pathlib.Path(store)/"dbond-probe")
        shutil.copy2(probe,signed_probe)
        subprocess.run(["codesign","--force","--sign","-",signed_probe],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        fixture=start([str(venv),str(root/"scripts/fixture.py")]); cleanup.callback(stop,fixture)
        port=int(read_line(fixture))
        def fresh():
            p=start([signed_probe,store,str(port)]); cleanup.callback(stop,p)
            return p,json.loads(read_line(p))["endpoint"]
        requests=fake_herald(pathlib.Path(store)/"fake-herald",cleanup)
        p,base=fresh(); a=tell(p,"build watcher"); b=tell(p,"second service")
        obj={"id":"event-1","payload":{"text":"Synthetic manifest ready"}}
        endpoint=a["endpoint"]; receive=endpoint+"/stream?after=0"
        assert call(endpoint,b["secret"],obj)[0] == 401
        with concurrent.futures.ThreadPoolExecutor() as pool:
            listener=pool.submit(call,receive,a["secret"])
            time.sleep(.12) # Only arrange this test so subscriber is present first.
            t=time.monotonic()
            status,ack=call(endpoint,a["secret"],obj)
            assert status==202 and ack["state"]=="queued"
            status,msgs=listener.result(10)
            elapsed=time.monotonic()-t
        msg=msgs["messages"][0]
        assert status==200 and msg["id"]==obj["id"] and msg["state"]=="local_reply" and msg["reply"]=="fixture:event-1"
        assert call(endpoint,a["secret"],obj)[1]["duplicate"] is True
        assert call(endpoint,a["secret"],dict(id=obj["id"],payload="different"))[0]==409
        assert call(receive,b["secret"])[0]==401
        assert call(b["endpoint"],b["secret"],obj)[0]==202
        other=call(b["endpoint"]+"/stream?after=0",b["secret"])[1]["messages"]
        assert len(other)==1 and other[0]["sequence"] != msg["sequence"]
        h=tell(p,"fixture-herald"); banner={"id":"banner-1","payload":{"title":"Render finished","body":"Synthetic clip"}}
        assert call(h["endpoint"],h["secret"],banner)[0]==202
        outcome=call(h["endpoint"]+"/stream?after=0",h["secret"])[1]["messages"][0]
        assert outcome["state"]=="herald_accepted" and len(requests)==1
        expected="dbond-"+hashlib.sha256((h["id"]+":banner-1").encode()).hexdigest()[:32]
        assert requests[0]["id"]==expected and requests[0]["app"]=="agent.dbond"
        assert call(h["endpoint"],h["secret"],banner)[1]["duplicate"]
        assert call(h["endpoint"],h["secret"],{"id":"reject","payload":dict(banner["payload"],command="do not run")})[0]==202
        rejected=call(h["endpoint"]+"/stream?after="+str(outcome["sequence"]),h["secret"])[1]["messages"][0]
        assert rejected["state"]=="herald_unconfirmed" and len(requests)==1
        push=json.dumps({"kind":"herald.notify","title":"Render finished","body":"Synthetic Dot push"},separators=(",",":"))
        assert tell(p,"inject-push "+h["id"]+" @dot:101 "+push)["injected"]
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            push_rows=call(h["endpoint"]+"/stream?after=0",h["secret"])[1]["messages"]
            if any(row["id"]=="herald-return-101" and row["state"]=="dot_stored" for row in push_rows): break
            time.sleep(.03)
        assert any(row["id"]=="@dot:101" and row["state"]=="herald_accepted" for row in push_rows)
        assert any(row["id"]=="herald-return-101" and row["reply"]=="fixture return @dot:101 stored" for row in push_rows)
        assert len(requests)==2 and requests[1]["app"]=="cloud.chatgpt"
        tell(p,"inject-push "+h["id"]+" @dot:101 "+push)
        assert len(requests)==2
        bad=json.dumps({"kind":"herald.notify","title":"No","body":"No","command":"extra"},separators=(",",":"))
        tell(p,"inject-push "+h["id"]+" @dot:103 "+bad)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            push_rows=call(h["endpoint"]+"/stream?after=0",h["secret"])[1]["messages"]
            if any(row["id"]=="@dot:103" and row["state"]=="herald_rejected" for row in push_rows): break
            time.sleep(.03)
        assert any(row["id"]=="@dot:103" and row["state"]=="herald_rejected" for row in push_rows)
        assert len(requests)==2
        tell(p,"inject-push "+h["id"]+" @dot:104 I cannot send that notification.")
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            push_rows=call(h["endpoint"]+"/stream?after=0",h["secret"])[1]["messages"]
            if any(row["id"]=="@dot:104" and row["state"]=="herald_rejected" for row in push_rows): break
            time.sleep(.03)
        assert any(row["id"]=="@dot:104" and row["state"]=="herald_rejected" for row in push_rows)
        assert len(requests)==2
        assert tell(p,"pause "+h["id"])["paused"]
        tell(p,"inject-push "+h["id"]+" @dot:102 "+push)
        time.sleep(.15)
        assert len(requests)==2
        statuses=tell(p,"status")
        p.stdin.write("quit\n");p.stdin.flush(); assert p.wait(5)==0
        p,base=fresh(); endpoint=base+"/v1/services/"+a["id"]+"/events"
        assert next(row for row in tell(p,"list")["services"] if row["id"]==h["id"])["paused"]
        assert len(requests)==2
        tell(p,"resume "+h["id"])
        deadline=time.monotonic()+8
        while len(requests)<3 and time.monotonic()<deadline: time.sleep(.03)
        assert len(requests)==3 and requests[2]["app"]=="cloud.chatgpt"
        h_url=base+"/v1/services/"+h["id"]+"/events"
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            push_rows=call(h_url+"/stream?after=0",h["secret"])[1]["messages"]
            if any(row["id"]=="herald-return-102" and row["state"]=="dot_stored" for row in push_rows): break
            time.sleep(.03)
        assert any(row["id"]=="@dot:102" and row["state"]=="herald_accepted" for row in push_rows)
        assert any(row["id"]=="herald-return-102" and row["reply"]=="fixture return @dot:102 stored" for row in push_rows)
        assert call(endpoint,a["secret"],obj)[1]["duplicate"] is True
        assert call(endpoint+"/stream?after=0",a["secret"])[1]["messages"]==[msg]
        assert tell(p,"remove "+a["id"])["removed"]
        assert call(endpoint,a["secret"],obj)[0]==401
        with sqlite3.connect(pathlib.Path(store)/"ledger.sqlite") as db:
            assert b["secret"] not in "".join(str(row) for row in db.execute("SELECT body FROM services"))
        print(json.dumps({"result":"pass","checks":["signed opaque installed-engine event, original payload via ACK and synthetic reply","blocked waiter immediately received correlated reply","replay/conflict differentiated","credential and cursor isolation","restart preserved pairing and receipt","removed credential denied","no bearer stored in ledger","opt-in Herald text-only adapter accepted by local fake; extra action rejected before send","Dot-initiated Herald push accepted once, strict envelope, paused restart replay, fixture-only correlated return"],"local_fixture_round_trip_ms":round(elapsed*1000,1),"production":"read-only health probes; no Dot or Herald message",**statuses},indent=2))
if __name__=="__main__": run()
