"""Validated plans, provenance, project state and crash-released POSIX locks.

This is orchestration, not a sandbox. Use a host sandbox for untrusted agents.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
import time

from . import __version__


class ForgeError(ValueError):
    pass


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    return h.hexdigest()


def regular_file(path):
    p=Path(path).absolute()
    if p.is_symlink() or not p.is_file():
        raise ForgeError('Expected regular file, not a symlink: '+str(p))
    # Canonicalise trusted parent aliases (macOS /var and /tmp are symlinks).
    # Parent replacement races require a host filesystem sandbox, not this check.
    return p.resolve()


def load_json(path):
    try:
        def unique(pairs):
            obj={}
            for k,v in pairs:
                if k in obj:raise ForgeError('Duplicate JSON key: '+k)
                obj[k]=v
            return obj
        return json.loads(Path(path).read_text(encoding='utf-8'),object_pairs_hook=unique,
                          parse_constant=lambda x:(_ for _ in ()).throw(ForgeError('Nonfinite JSON')))
    except (OSError,UnicodeError,json.JSONDecodeError) as exc:
        raise ForgeError('Cannot read JSON: '+str(path)) from exc


def atomic_json(path, value):
    path=Path(path)
    if path.is_symlink():raise ForgeError('Refusing symlink destination')
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.forge-write-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n');f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def relative_path(value):
    if not isinstance(value,str) or not value or '\\' in value or '\x00' in value:raise ForgeError('Expected portable relative path')
    p=PurePosixPath(value)
    if p.is_absolute() or any(x in ('..','.forge','.git') for x in p.parts) or value in ('.',''):
        raise ForgeError('Unsafe project path: '+value)
    if str(p)!=value:raise ForgeError('Use canonical relative paths: '+value)
    return value


def project_path(root, rel):
    relative_path(rel);root=Path(root).resolve();target=root/rel
    for item in [target,*target.parents]:
        if item==root:break
        if item.is_symlink():raise ForgeError('Symlink in project output: '+rel)
    if not target.resolve().is_relative_to(root):raise ForgeError('Output escapes project')
    return target


def output_hashes(root, outputs):
    result={}
    for rel in outputs:
        p=project_path(root,rel)
        if not p.is_file():raise ForgeError('Missing regular output: '+rel)
        result[rel]=file_digest(p)
    return result


def tree_hash(root):
    root=Path(root).resolve();files={}
    for p in sorted(root.rglob('*')):
        rel=p.relative_to(root)
        if any(x in ('.forge','.git','__pycache__','.pytest_cache') for x in rel.parts):continue
        if p.is_symlink():raise ForgeError('Project tree contains symlink: '+str(rel))
        if p.is_file():files[rel.as_posix()]=file_digest(p)
    return digest(canonical(files).encode())


def number(value,low,high,name):
    try:valid=not isinstance(value,bool) and isinstance(value,(int,float)) and low<=value<=high and math.isfinite(value)
    except OverflowError:valid=False
    if not valid:
        raise ForgeError('Invalid '+name)
    return value


def text(value,name,limit=20000):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or '\x00' in value:raise ForgeError('Invalid '+name)
    return value


def validate_plan(plan):
    if not isinstance(plan,dict) or plan.get('schema')!=1 or type(plan.get('schema')) is not int:raise ForgeError('Plan schema must be 1')
    if set(plan)-{'schema','goal','tasks','checks','visual_review_required','protected_files'}:raise ForgeError('Unknown plan fields')
    text(plan.get('goal'),'goal')
    tasks=plan.get('tasks');checks=plan.get('checks')
    if not isinstance(tasks,list) or not 1<=len(tasks)<=50:raise ForgeError('Plan needs 1..50 tasks')
    if not isinstance(checks,list) or not 1<=len(checks)<=50:raise ForgeError('Plan needs independent checks')
    ids=set();outputs=set()
    for t in tasks:
        if not isinstance(t,dict) or set(t)-{'id','brief','requires','outputs'}:raise ForgeError('Invalid task')
        task_id=text(t.get('id'),'task id',64)
        if not re.fullmatch('[a-zA-Z0-9_-]+',task_id) or task_id in ids:raise ForgeError('Invalid/duplicate task id')
        text(t.get('brief'),'task brief')
        deps=t.get('requires',[])
        if not isinstance(deps,list) or any(not isinstance(x,str) or x not in ids for x in deps):raise ForgeError('Dependencies must precede task')
        items=t.get('outputs')
        if not isinstance(items,list) or not items:raise ForgeError('Task must declare outputs')
        for rel in items:
            relative_path(rel)
            if rel in outputs:raise ForgeError('Multiple owners for output '+rel)
            outputs.add(rel)
        ids.add(task_id)
    check_ids=set()
    for c in checks:
        if not isinstance(c,dict) or set(c)-{'id','argv','timeout_seconds'}:raise ForgeError('Invalid check')
        cid=text(c.get('id'),'check id',64)
        if cid in check_ids:raise ForgeError('Duplicate check id')
        check_ids.add(cid)
        a=c.get('argv')
        if not isinstance(a,list) or not 1<=len(a)<=100:raise ForgeError('Check argv required')
        for x in a:text(x,'argv element',4096)
        number(c.get('timeout_seconds',30),.01,600,'check timeout')
    if type(plan.get('visual_review_required',False)) is not bool:raise ForgeError('visual_review_required must be bool')
    protected=plan.get('protected_files',[])
    if not isinstance(protected,list) or len(protected)>100:raise ForgeError('Invalid protected files')
    for p in protected:
        text(p,'protected path',4096)
        if not Path(p).is_absolute():raise ForgeError('Protected files must be absolute')
    return plan


def project_config(root):
    path=project_path(root,'forge.json');config=load_json(path)
    if not isinstance(config,dict) or type(config.get('schema')) is not int or config.get('schema')!=1:raise ForgeError('Unsupported project schema')
    if config.get('engine_version')!=__version__:raise ForgeError('Engine pin mismatch; explicit upgrade required')
    if config.get('name_source')=='folder':config={**config,'name':Path(root).resolve().name}
    return config


def protect(plan_path,plan,root):
    root=Path(root).resolve();paths=[Path(plan_path),*[Path(x) for x in plan.get('protected_files',[])]];result={}
    for p in paths:
        p=regular_file(p).resolve()
        if p.is_relative_to(root):raise ForgeError('Plan/evaluator evidence must live outside builder project')
        result[str(p)]=file_digest(p)
    return result


def verify_protected(snapshot):
    for name,expected in snapshot.items():
        p=Path(name)
        regular_file(p)
        if file_digest(p)!=expected:raise ForgeError('Protected evidence changed: '+name)


@contextmanager
def project_lock(root):
    state=Path(root)/'.forge'
    if state.is_symlink():raise ForgeError('Refusing symlink state directory')
    state.mkdir(exist_ok=True)
    lock=state/'lock'
    if lock.is_symlink():raise ForgeError('Refusing symlink lock')
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise ForgeError('Another FORGE process owns this project') from exc
        yield state
    finally:os.close(fd)


class State:
    def __init__(self,path):
        path=Path(path)
        if path.is_symlink():raise ForgeError('Refusing symlink database')
        self.db=sqlite3.connect(path,timeout=5)
        version=self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0,1):self.db.close();raise ForgeError('Unsupported state schema; preserve DB and upgrade explicitly')
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS tasks (fingerprint TEXT, id TEXT, hashes TEXT, PRIMARY KEY(fingerprint,id))')
            self.db.execute('CREATE TABLE IF NOT EXISTS checkpoints (fingerprint TEXT PRIMARY KEY, tree TEXT, prefix INTEGER)')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, kind TEXT, detail TEXT)')
            self.db.execute('PRAGMA user_version=1')
    def event(self,kind,detail):
        with self.db:self.db.execute('INSERT INTO events(time,kind,detail) VALUES(?,?,?)',(time.time(),kind,canonical(detail)))
    def complete(self,key,tid,hashes,checkpoint=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO tasks VALUES(?,?,?)',(key,tid,canonical(hashes)))
            if checkpoint is not None:
                self.db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?)',(key,*checkpoint))
    def resume_prefix(self,key,tree):
        row=self.db.execute('SELECT prefix FROM checkpoints WHERE fingerprint=? AND tree=?',(key,tree)).fetchone()
        return row[0] if row else 0
    def invalidate_checkpoints(self):
        with self.db:
            self.db.execute('DELETE FROM checkpoints')
    def reusable(self,key,tid,hashes):
        r=self.db.execute('SELECT hashes FROM tasks WHERE fingerprint=? AND id=?',(key,tid)).fetchone()
        return bool(r and json.loads(r[0])==hashes)
    def close(self):self.db.close()
