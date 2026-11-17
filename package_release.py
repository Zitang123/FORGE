"""Create and verify the deliverable ZIP; no Git/network/publication actions."""
import hashlib
import json
from pathlib import Path
import stat
import sys
import zipfile

ROOT=Path(__file__).resolve().parent


def included(path):
    rel = path.relative_to(ROOT)
    if any(part.startswith('.') or part == '__pycache__' for part in rel.parts):
        return False
    if rel.parts[0] == 'dist':
        return rel.as_posix() in {'dist/forge-3.1.0.pyz', 'dist/engine-manifest.json'}
    if rel.parts[0] in {'engine', 'docs', 'samples', 'benchmarks'}:
        return path.suffix in {'.py', '.md', '.json', '.html', '.mjs'}
    return rel.as_posix() in {
        'README.md', 'AGENTS.md', 'build.py', 'launch.py',
        'Start_Observatory.command', 'Start_Demo.command', 'package_release.py',
    }


def main():
    target=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else ROOT.parent/'outputs/FORGE_v3.1.0.zip'
    if target.is_relative_to(ROOT):raise SystemExit('Choose an archive path outside the source tree')
    paths=[]
    for p in sorted(ROOT.rglob('*')):
        if (p.is_file() or p.is_symlink()) and included(p):
            if p.is_symlink():raise SystemExit('Refusing symlink in release: '+str(p))
            paths.append(p)
    sys.path.insert(0,str(ROOT/'engine'))
    from forge.cli import STARTER_AGENTS
    from forge.portable import BEGIN, END
    # A clean application root wraps the separately reusable engine capsule.
    entries={'FORGE/'+p.relative_to(ROOT).as_posix():p.read_bytes() for p in paths}
    new_config={'schema':1,'engine_version':'3.1.0','name':'New project','name_source':'folder','specialization':'general',
                'brief':'Agree the audience, product vision and first useful outcome with the user before implementation.'}
    entries.update({
        'AGENTS.md':(BEGIN+'\n'+STARTER_AGENTS+END+'\n').encode(),
        'PROJECT.md':b'# Project vision\n\nThe project name follows this folder. Discuss the audience, problem, first useful outcome, design direction and material constraints before implementation; record confirmed decisions here. No product has been built in this fresh project.\n',
        'forge.json':(json.dumps(new_config,indent=2)+'\n').encode(),
        'acceptance.json':(json.dumps({'schema':1,'criteria':[],'status':'needs_intake'},indent=2)+'\n').encode(),
        'memory/NOW.md':b'# Current state\n\nFresh FORGE project. Settle the essential vision, then start the project Observatory immediately before building. Return its verified URL before implementation and at every final handoff. Preserve unrelated user work.\n',
        'Start_Observatory.command':b'#!/bin/zsh\nset -eu\ncd -- "$(dirname -- "$0")"\nexec python3 FORGE/launch.py --project . "$@"\n',
        'Start_Demo.command':b'#!/bin/zsh\nset -eu\ncd -- "$(dirname -- "$0")"\nexec python3 FORGE/launch.py --demo "$@"\n',
        'UI_UX.md':(ROOT/'docs/UI_UX.md').read_bytes(),
        '.gitignore':b'.env\n.env.*\n!.env.example\n.forge/\n__pycache__/\n*.pyc\n.DS_Store\n',
        'README_FIRST.md':'''# Your FORGE project

1. Rename this outer folder to your project name if you want.
2. Double-click Start_Observatory.command. Python3.11+ is required; no package install is needed.
3. Keep the Terminal window open. The launcher prints the actual project URL and opens it in your browser.
4. Open THIS outer folder in Codex. Discuss the vision, then say you are ready to build. Its instructions require the Observatory link before implementation and again at every final handoff.

The network map is the main view. Drag agent rings, drag empty space to pan, scroll or use +/- to zoom, and use Fit/Reset. Statistics and selected-agent details are in the right sidebar (a collapsible sheet on small screens). Full labels are available by selecting a node. Only real recorded activity animates.

The project gets its own persistent ID on first launch. Renaming or moving the whole outer folder preserves identity, history and the relative engine connection; relaunch after moving it. Folder-sourced project names follow the new name. The default project port remains stable, retaining browser-saved layout. If the port is occupied, run the launcher with --port 0 to choose a free one; a different origin has a separate browser layout.

Keep your application files at this outer level; the reusable engine, documentation and historical evidence are under FORGE/. Start_Demo.command opens the included completed sample separately.

For an existing project, move only the inner FORGE folder into it, then double-click FORGE/Start_Observatory.command. Setup preserves existing briefs/source and appends a managed instruction block without changing existing user text. It refuses incompatible pins or conflicting setup files rather than overwrite them.

For Terminal: python3 FORGE/launch.py --project .
For setup without a server: python3 FORGE/launch.py --project . --prepare-only
After setup, the rename-safe CLI is: python3 .forge/forge.py

Localhost works while the server is running. See FORGE/docs/OBSERVATORY.md for setup and controls.
'''.encode(),
    })
    manifest={'schema':1,'release':'FORGE 3.1.0 public release','files':{n:hashlib.sha256(b).hexdigest() for n,b in sorted(entries.items())},
              'note':'Manifest excludes itself. No runtime project UUID/URL/absolute engine path is assigned to the fresh root. Runtime secrets and Git metadata are excluded; demo runtime state is generated locally.'}
    manifest_bytes=(json.dumps(manifest,indent=2)+'\n').encode()
    entries['RELEASE_MANIFEST.json']=manifest_bytes
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,content in sorted(entries.items()):
            info=zipfile.ZipInfo('FORGE_Project/'+name,date_time=(2026,11,10,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.create_system=3
            permissions=0o755 if Path(name).suffix in ('.command','.pyz') else 0o644
            info.external_attr=(stat.S_IFREG|permissions)<<16
            z.writestr(info,content)
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        for name,expected in manifest['files'].items():
            assert hashlib.sha256(z.read('FORGE_Project/'+name)).hexdigest()==expected
    result={'file':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'files':len(entries),'manifest_verified':True}
    print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
