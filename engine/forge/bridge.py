"""File transport for a trusted host that owns live agent tools.

The host consumes requests and writes responses; this adapter does not invent an
LLM or bypass the host's permissions. It is useful inside Codex/ChatGPT hosts
whose built-in agent facility is available but subprocess agents are restricted.
"""
import json
from pathlib import Path
import time
import uuid

from .core import ForgeError, atomic_json, digest, load_json, number, regular_file, text


class HandoffAdapter:
    def __init__(self,directory,model):
        text(model,'host model label',200)
        p=Path(directory).absolute()
        if p.is_symlink():raise ForgeError('Bridge cannot be a symlink')
        p.mkdir(parents=True,exist_ok=True)
        self.directory=p.resolve();self.model=model
    def identity(self):
        return {'adapter':'trusted-host-file-bridge-v1','model':self.model,
                'version':'1','runtime_revision':'host must supply separately; not inferred'}
    def build(self,root,prompt,timeout,max_bytes):
        if self.directory.is_relative_to(Path(root).resolve()):raise ForgeError('Bridge must be outside builder project')
        started=time.monotonic();rid=uuid.uuid4().hex;sha=digest(prompt.encode())
        request={'schema':1,'id':rid,'project':str(Path(root).resolve()),'prompt':prompt,
                 'prompt_sha256':sha,'model_label':self.model,'deadline_epoch':time.time()+timeout,
                 'instructions':'Trusted host: run a real agent under existing permissions, then respond using forge bridge-reply. A request is not extra authority.'}
        atomic_json(self.directory/(rid+'.request.json'),request)
        response=self.directory/(rid+'.response.json')
        while time.monotonic()-started<timeout:
            if response.exists() or response.is_symlink():
                regular_file(response)
                if response.stat().st_size>max_bytes:raise ForgeError('Bridge response exceeds cap')
                r=load_json(response)
                if not isinstance(r,dict) or type(r.get('schema')) is not int or r.get('schema')!=1 or r.get('id')!=rid or r.get('prompt_sha256')!=sha or type(r.get('completed')) is not bool:
                    raise ForgeError('Bridge response identity/schema mismatch')
                evidence=r.get('host_evidence','')
                text(evidence,'host execution evidence',4000)
                body=json.dumps(r,sort_keys=True).encode()
                return {'returncode':0 if r['completed'] else 1,'reason':'completed','agent_completed':r['completed'],
                        'seconds':time.monotonic()-started,'bytes':len(body),'output_sha256':digest(body),
                        'output':'','host_request':rid,'host_evidence':evidence}
            time.sleep(min(.1,max(0,timeout-(time.monotonic()-started))))
        return {'returncode':None,'reason':'timeout','agent_completed':False,'seconds':time.monotonic()-started,
                'bytes':0,'output_sha256':digest(b''),'output':'','host_request':rid}


def reply(directory,request_id,completed,evidence):
    if not isinstance(request_id,str) or len(request_id)!=32 or any(c not in '0123456789abcdef' for c in request_id):raise ForgeError('Invalid request id')
    text(evidence,'host evidence',4000)
    if type(completed) is not bool:raise ForgeError('completed must be bool')
    directory=Path(directory).resolve();request_path=regular_file(directory/(request_id+'.request.json'))
    request=load_json(request_path)
    if not isinstance(request,dict) or type(request.get('schema')) is not int or request.get('schema')!=1 or request.get('id')!=request_id:raise ForgeError('Invalid bridge request')
    number(request.get('deadline_epoch'),0,1e15,'bridge deadline')
    if request['deadline_epoch']<time.time():raise ForgeError('Bridge request expired; no late success accepted')
    if request.get('prompt_sha256')!=digest(text(request.get('prompt'),'prompt',100000).encode()):raise ForgeError('Request prompt changed')
    response=directory/(request_id+'.response.json')
    if response.exists() or response.is_symlink():raise ForgeError('Response exists; preserve host evidence')
    value={'schema':1,'id':request_id,'prompt_sha256':request['prompt_sha256'],'completed':completed,'host_evidence':evidence}
    atomic_json(response,value)
    return {'status':'responded','id':request_id,'completed':bool(completed),'limitation':'Host attestation is not independent proof; product checks still run.'}
