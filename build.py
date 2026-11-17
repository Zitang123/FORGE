"""Build a relocatable stdlib-only Python zipapp; no dependency installation."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT=Path(__file__).resolve().parent
def build():
    target=ROOT/'dist/forge-3.1.0.pyz';target.parent.mkdir(exist_ok=True)
    target.write_bytes(b'#!/usr/bin/env python3\n')
    entries={p.relative_to(ROOT/'engine').as_posix():p.read_bytes() for p in sorted((ROOT/'engine/forge').glob('*.py'))}
    entries.update({p.relative_to(ROOT/'engine').as_posix():p.read_bytes() for p in sorted((ROOT/'engine/forge/web').glob('*.html'))})
    entries['forge/UI_UX.md']=(ROOT/'docs/UI_UX.md').read_bytes()
    entries['__main__.py']=b'from forge.cli import entrypoint\nentrypoint()\n'
    with zipfile.ZipFile(target,'a',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,content in sorted(entries.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,11,10,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            info.create_system=3;info.external_attr=0o100644<<16;archive.writestr(info,content)
    target.chmod(0o755)
    info={'engine_version':'3.1.0','file':target.name,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'bytes':target.stat().st_size}
    (ROOT/'dist/engine-manifest.json').write_text(json.dumps(info,indent=2)+'\n')
    print(json.dumps(info))
if __name__=='__main__':build()
