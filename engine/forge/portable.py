"""Preserving local attachment and relative engine routing for project capsules."""
import os
from pathlib import Path
import re
import sys
import uuid

from . import __version__
from .core import ForgeError, atomic_json, load_json, project_config, regular_file, text


BEGIN='<!-- FORGE:BEGIN -->'
END='<!-- FORGE:END -->'
SHIM='''#!/usr/bin/env python3
"""FORGE relative engine shim. Application code stays outside the shared engine."""
import json
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
try:
    config=json.loads((root/'.forge/engine.json').read_text())
    engine=(root/config['engine_relative']).resolve()
    if not engine.is_file(): raise ValueError('Engine missing; rerun the project launcher')
except (OSError,ValueError,KeyError,TypeError) as exc:
    print('forge: '+str(exc),file=sys.stderr);raise SystemExit(2)
raise SystemExit(subprocess.call([sys.executable,str(engine),*sys.argv[1:]],cwd=root))
'''


def stable_identity(root):
    """Assign a persistent ID once; callers explicitly authorise local setup."""
    root=Path(root).resolve();config=project_config(root)
    value=config.get('project_id')
    if value is not None:
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):raise ForgeError('Invalid persistent project ID')
        return value
    # Read stored bytes rather than the derived folder-name view of configuration.
    stored=load_json(root/'forge.json');stored['project_id']=uuid.uuid4().hex
    atomic_json(root/'forge.json',stored)
    return stored['project_id']


def attach(root,engine_path,name=None):
    from .cli import STARTER_AGENTS
    raw_root=Path(root).absolute()
    if raw_root.is_symlink() or not raw_root.is_dir():raise ForgeError('Project must be an existing directory, not a symlink')
    root=raw_root.resolve();engine=regular_file(engine_path)
    if name is not None:text(name,'project name',120)
    # Preflight all writable targets before any modification.
    targets=['forge.json','AGENTS.md','PROJECT.md','acceptance.json','memory/NOW.md','.forge/engine.json','.forge/forge.py']
    for rel in targets:
        path=root/rel
        for part in [path,*path.parents]:
            if part==root:break
            if part.is_symlink():raise ForgeError('Refusing symlink setup target: '+rel)
        if path.exists() and not path.is_file():raise ForgeError('Setup target is not a file: '+rel)
    for rel in ('memory','.forge'):
        path=root/rel
        if path.exists() and not path.is_dir():raise ForgeError('Setup directory is not a directory: '+rel)
    agents=root/'AGENTS.md';existing=agents.read_bytes().decode('utf-8') if agents.exists() else ''
    if existing.count(BEGIN)!=existing.count(END) or existing.count(BEGIN)>1:
        raise ForgeError('Ambiguous managed instructions; existing user content preserved')
    if BEGIN in existing and existing.index(BEGIN)>existing.index(END):raise ForgeError('Invalid managed instruction order')
    shim=root/'.forge/forge.py'
    if shim.exists() and shim.read_text()!=SHIM:raise ForgeError('Existing shim differs; user content preserved')
    config_path=root/'forge.json'
    if config_path.exists():project_config(root)
    else:atomic_json(config_path,{'schema':1,'engine_version':__version__,'project_id':uuid.uuid4().hex,
                                  'name':name or root.name,'name_source':'explicit' if name else 'folder',
                                  'specialization':'general','brief':'Settle the essential vision and first useful outcome with the user before implementation.'})
    stable_identity(root)
    created=[]
    if BEGIN not in existing:
        addition=('\n' if existing.endswith('\n') else '\n\n') if existing else ''
        addition+=BEGIN+'\n'+STARTER_AGENTS+END+'\n'
        # Append without normalising/truncating any pre-existing user bytes.
        with agents.open('ab') as stream:
            stream.write(addition.encode('utf-8'));stream.flush();os.fsync(stream.fileno())
        created.append('AGENTS.md managed contract')
    defaults={'PROJECT.md':'# '+(name or root.name)+'\n\nDiscuss the audience, problem, first useful outcome and material constraints before implementation. Record confirmed decisions and observable acceptance criteria.\n',
              'memory/NOW.md':'# Current state\n\nProject attached to FORGE. Confirm the first useful outcome, preserve existing files, and share the verified project Observatory link immediately before building and at every final handoff.\n'}
    for rel,content in defaults.items():
        path=root/rel
        if not path.exists():path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content,encoding='utf-8');created.append(rel)
    guide=root/'UI_UX.md'
    if not guide.exists() and not guide.is_symlink():
        from importlib.resources import files
        resource=files('forge').joinpath('UI_UX.md')
        if resource.is_file():
            with guide.open('xb') as stream:
                stream.write(resource.read_bytes())
            created.append('UI_UX.md')
    if not (root/'acceptance.json').exists():
        atomic_json(root/'acceptance.json',{'schema':1,'criteria':[],'status':'needs_intake'});created.append('acceptance.json')
    state=root/'.forge';state.mkdir(exist_ok=True)
    atomic_json(state/'engine.json',{'schema':1,'engine_version':__version__,'engine_relative':Path(os.path.relpath(engine,root)).as_posix()})
    if not shim.exists():shim.write_text(SHIM,encoding='utf-8');shim.chmod(0o755)
    from .telemetry import project_identity
    return {'status':'attached','project':project_identity(root),'created':created,'engine_relative':load_json(state/'engine.json')['engine_relative'],
            'event_command':[sys.executable,'.forge/forge.py','event','.'],
            'observe_command':[sys.executable,'.forge/forge.py','observe','.','--port','0','--open']}
