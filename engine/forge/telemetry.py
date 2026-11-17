"""Project-scoped, read-only loopback observatory and explicit host events.

No account/session scraping or inferred tool activity. The engine and a trusted
host agent must emit actual events. HTTP never executes commands or reads arbitrary
paths. The server is intentionally available only on the local machine.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit

from .core import ForgeError, State, atomic_json, project_config, text

EVENT_TYPES={'session.started','session.completed','agent.started','agent.progress','agent.completed','agent.failed',
             'stage.started','stage.completed','check.passed','check.failed','heartbeat','idle'}


def project_identity(root):
    root=Path(root).resolve();config=project_config(root)
    value=config.get('project_id')
    if value is not None and (not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value)):raise ForgeError('Invalid persistent project ID')
    return {'id':value or hashlib.sha256(str(root).encode()).hexdigest()[:16],'name':config.get('name','FORGE project')}


def emit(root,kind,agent_id='codex',parent_id=None,label=None,tokens=None):
    root=Path(root).resolve();project_config(root)
    if not isinstance(kind,str) or kind not in EVENT_TYPES:raise ForgeError('Unknown telemetry event type')
    for value in (agent_id,parent_id):
        if value is not None and (not isinstance(value,str) or not re.fullmatch('[a-zA-Z0-9_.:-]{1,80}',value)):
            raise ForgeError('Invalid telemetry agent id')
    if agent_id is None:raise ForgeError('Agent id required')
    if label is not None:text(label,'event label',240)
    if tokens is not None and (type(tokens) is not int or not 0<=tokens<=10**12):raise ForgeError('Token count must be observed nonnegative integer')
    state_dir=root/'.forge'
    if state_dir.is_symlink():raise ForgeError('Refusing symlink state directory')
    state_dir.mkdir(exist_ok=True)
    state=State(state_dir/'state.sqlite')
    try:state.event('telemetry',{'type':kind,'agent_id':agent_id,'parent_id':parent_id,'label':label or kind,'tokens':tokens})
    finally:state.close()
    return {'status':'recorded','project':project_identity(root),'type':kind,'agent_id':agent_id}


def snapshot(root,now=None):
    root=Path(root).resolve();project=project_identity(root);now=time.time() if now is None else now
    state_dir=root/'.forge';db_path=state_dir/'state.sqlite';rows=[];count=0;warning=None
    if state_dir.is_symlink() or db_path.is_symlink():raise ForgeError('Refusing symlink telemetry state')
    if db_path.exists():
        db=None
        try:
            db=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True,timeout=.2)
            if db.execute('PRAGMA user_version').fetchone()[0]!=1:raise ForgeError('Unsupported telemetry schema')
            count=db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            rows=list(reversed(db.execute('SELECT id,time,kind,detail FROM events ORDER BY id DESC LIMIT 2000').fetchall()))
        except sqlite3.Error:
            warning='Telemetry database temporarily unavailable'
        finally:
            if db is not None:db.close()
    agents={};events=[];started=0;passed=0;failed=0;token_total=0;have_tokens=False;session_start=None;last_end=None;saw_boundary=False;saw_start=False
    def agent(aid,label,parent,stamp,status,restart=False):
        if aid not in agents:
            agents[aid]={'id':aid,'label':label,'parent_id':parent,'status':status,'started_at':stamp,'updated_at':stamp,'completed_at':None}
        item=agents[aid];item.update(status=status,updated_at=stamp)
        if restart:item.update(label=label,parent_id=parent,started_at=stamp,completed_at=None)
        elif status=='running':item['completed_at']=None
        if status in ('completed','error','idle'):item['completed_at']=stamp
        return item
    for eid,stamp,kind,raw in rows:
        if isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not math.isfinite(stamp):continue
        try:detail=json.loads(raw)
        except (TypeError,json.JSONDecodeError):continue
        if not isinstance(detail,dict):continue
        typ=None;aid=None;parent=None;label=''
        if kind=='telemetry':
            typ=detail.get('type');aid=detail.get('agent_id');parent=detail.get('parent_id');label=str(detail.get('label',''))[:240]
            if not isinstance(typ,str) or typ not in EVENT_TYPES or not isinstance(aid,str):continue
            if typ=='session.started':
                saw_boundary=True;saw_start=True
                # Display the current prompt/session graph rather than accumulate unrelated runs.
                agents={};started=0;passed=0;failed=0;token_total=0;have_tokens=False;session_start=stamp;last_end=None
                agent(aid,label,parent,stamp,'running',restart=True)
            elif typ in ('agent.started','stage.started'):
                agent(aid,label,parent,stamp,'running',restart=True);started+=typ=='agent.started'
            elif typ in ('agent.progress','heartbeat'):
                item=agent(aid,label,parent,stamp,'running')
                if typ=='agent.progress':item['label']=label
            elif typ in ('agent.completed','stage.completed'):agent(aid,label,parent,stamp,'completed')
            elif typ=='agent.failed':agent(aid,label,parent,stamp,'error')
            elif typ=='idle':agent(aid,label,parent,stamp,'idle')
            elif typ=='session.completed':
                saw_boundary=True
                for item in agents.values():
                    if item['status']=='running':item.update(status='completed',completed_at=stamp,updated_at=stamp)
                last_end=stamp
            if typ=='check.passed':passed+=1
            if typ=='check.failed':failed+=1
            observed=detail.get('tokens')
            if type(observed) is int and observed>=0:token_total+=observed;have_tokens=True
        elif kind=='run_started':
            saw_boundary=True;saw_start=True
            typ='session.started';aid='coordinator';label='FORGE run'
            agents={};started=0;passed=0;failed=0;token_total=0;have_tokens=False;session_start=stamp;last_end=None
        elif kind=='agent_started':
            if last_end is not None:
                agents={};started=0;passed=0;failed=0;token_total=0;have_tokens=False;session_start=stamp;last_end=None
            typ='agent.started';aid='engine-'+str(eid);parent='coordinator';label='Build · '+str(detail.get('task','task'))[:100]
            agent(aid,label,parent,stamp,'running');started+=1
            agents[aid]['task_id']=detail.get('task')
            if session_start is None or last_end is not None:session_start=stamp;last_end=None
        elif kind=='agent_finished':
            typ='agent.completed' if detail.get('status')=='built' else 'agent.failed'
            matches=[a for a in agents.values() if a.get('task_id')==detail.get('id') and a['status']=='running']
            aid=matches[-1]['id'] if matches else 'engine-finish-'+str(eid);parent='coordinator';label='Build · '+str(detail.get('id','task'))[:100]
            agent(aid,label,parent,stamp,'completed' if typ=='agent.completed' else 'error')
            process=detail.get('process');usage=process.get('usage',{}) if isinstance(process,dict) else {}
            if not isinstance(usage,dict):usage={}
            observed=[usage.get('input_tokens'),usage.get('output_tokens')]
            if all(type(x) is int and x>=0 for x in observed):token_total+=sum(observed);have_tokens=True
        elif kind=='check_started':
            if last_end is not None:
                agents={};started=0;passed=0;failed=0;token_total=0;have_tokens=False;session_start=stamp;last_end=None
            typ='stage.started';aid='check-'+str(eid);parent='coordinator';label='Verify · '+str(detail.get('id','check'))[:100]
            agent(aid,label,parent,stamp,'running');agents[aid]['check_id']=detail.get('id')
        elif kind=='check':
            typ='check.passed' if detail.get('status')=='pass' else 'check.failed';label='Verify · '+str(detail.get('id','check'))[:100]
            passed+=typ=='check.passed';failed+=typ=='check.failed'
            matches=[a for a in agents.values() if a.get('check_id')==detail.get('id') and a['status']=='running']
            if matches:aid=matches[-1]['id'];agent(aid,label,'coordinator',stamp,'completed' if typ=='check.passed' else 'error')
        elif kind=='run_finished':
            saw_boundary=True
            typ='session.completed';label='Run · '+str(detail.get('status','finished'));last_end=stamp
            for item in agents.values():
                if item['status']=='running':item.update(status='completed' if detail.get('status') in ('verified','awaiting_review') else 'error',completed_at=stamp,updated_at=stamp)
        if typ:
            events.append({'id':eid,'time':stamp,'type':typ,'agent_id':aid,'parent_id':parent,'label':label,'detail':{}})
    last=events[-1]['time'] if events else None
    running=[a for a in agents.values() if a['status']=='running']
    for a in running:
        if now-a['updated_at']>120:a['status']='stale'
    incomplete=count>len(rows) and not saw_boundary
    totals_incomplete=count>len(rows) and not saw_start
    if warning:status='error'
    elif incomplete:status='stale';warning='Recent history is incomplete; current activity and session totals cannot be confirmed.'
    elif running:status='running' if any(a['status']=='running' for a in running) else 'stale'
    else:status='idle'
    if totals_incomplete and warning is None:warning='Session start is outside retained history; session totals are unavailable.'
    elapsed=max(0,(last_end if last_end is not None else now)-(session_start if session_start is not None else now))
    return {'schema':1,'project':project,'status':status,'server_time':now,'last_event':last,'agents':list(agents.values())[-24:],
            'stats':{'agents_started':None if totals_incomplete else started,'events':count,'checks_passed':None if totals_incomplete else passed,'checks_failed':None if totals_incomplete else failed,'elapsed_seconds':None if totals_incomplete else round(elapsed,2),
                     'tokens':token_total if have_tokens and not totals_incomplete else None},'events':events[-100:],'warning':warning}


def handler_for(root,port):
    root=Path(root).resolve();identity=project_identity(root);prefix='/p/'+identity['id']+'/'
    html=files('forge').joinpath('web/index.html').read_bytes()
    scripts=re.findall(rb'<script>([\s\S]*?)</script>',html)
    hashes=' '.join("'sha256-"+__import__('base64').b64encode(hashlib.sha256(s).digest()).decode()+"'" for s in scripts)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,body=b'',kind='application/json; charset=utf-8',headers=None):
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src "+hashes+"; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            for k,v in (headers or {}).items():self.send_header(k,v)
            self.end_headers()
            if body:self.wfile.write(body)
        def allowed(self):
            hosts={'127.0.0.1:'+str(port),'localhost:'+str(port)}
            if self.headers.get('Host') not in hosts:return False
            origin=self.headers.get('Origin')
            return origin is None or origin in {'http://'+h for h in hosts}
        def do_GET(self):
            if not self.allowed():self.send(403,b'{"error":"local same-origin access only"}');return
            path=urlsplit(self.path).path
            if path=='/':self.send(303,headers={'Location':prefix});return
            if path==prefix:self.send(200,html,'text/html; charset=utf-8');return
            if path==prefix+'api/state':
                try:body=json.dumps(snapshot(root),allow_nan=False).encode();self.send(200,body)
                except (ForgeError,OSError,ValueError):self.send(503,b'{"error":"project telemetry unavailable"}')
                return
            self.send(404,b'{"error":"not found"}')
        def do_POST(self):self.send(405,b'{"error":"read-only observatory"}')
        do_PUT=do_DELETE=do_PATCH=do_POST
    return Handler


def serve(root,port=8765,open_browser=False,engine_command=None):
    from .portable import stable_identity
    root=Path(root).resolve();stable_identity(root);identity=project_identity(root)
    if type(port) is not int or not 0<=port<=65535:raise ForgeError('Port must be 0..65535')
    # Bind before constructing the final handler so an ephemeral port is known.
    server=ThreadingHTTPServer(('127.0.0.1',port),BaseHTTPRequestHandler)
    try:
        actual=server.server_address[1];server.RequestHandlerClass=handler_for(root,actual)
        url='http://127.0.0.1:'+str(actual)+'/p/'+identity['id']+'/'
        state_dir=root/'.forge'
        if state_dir.is_symlink():raise ForgeError('Refusing symlink state directory')
        state_dir.mkdir(exist_ok=True)
        shim=root/'.forge/forge.py'
        command=[sys.executable,str(shim)] if shim.is_file() else engine_command or [sys.executable,'-m','forge']
        atomic_json(state_dir/'observatory.json',{'schema':1,'project_id':identity['id'],'url':url,'pid':os.getpid(),
                    'event_command':command+['event',str(root)],'note':'Emit only actual project activity. This file does not imply the server is still running.'})
        print(json.dumps({'status':'serving','url':url,'project':identity,'pid':os.getpid()}),flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        server.serve_forever(poll_interval=.25)
    finally:server.server_close()
