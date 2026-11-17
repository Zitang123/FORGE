#!/usr/bin/env python3
"""Attach this portable engine to a project and open its local Observatory."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

BUNDLE=Path(__file__).resolve().parent


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--project',help='Target project directory; defaults to the parent of this engine folder')
    group.add_argument('--demo',action='store_true',help='Open the included completed mathematics demo')
    parser.add_argument('--port',type=int,help='Override stable project port; 0 selects a free port')
    parser.add_argument('--no-open',action='store_true')
    parser.add_argument('--prepare-only',action='store_true',help='Attach and print commands without starting a server')
    args=parser.parse_args(argv)
    try:
        if args.port is not None and not 0<=args.port<=65535:raise ValueError('Port must be 0..65535')
        engine=BUNDLE/'dist/forge-3.1.0.pyz'
        if not engine.is_file():raise ValueError('Shared engine missing: '+str(engine))
        project=(BUNDLE/'samples/observatory-demo') if args.demo else Path(args.project).resolve() if args.project else BUNDLE.parent
        result=subprocess.run([sys.executable,str(engine),'attach',str(project),'--engine',str(engine)],capture_output=True,text=True,timeout=30)
        if result.returncode:
            print(result.stderr.strip() or 'FORGE project setup failed',file=sys.stderr);return result.returncode
        setup=json.loads(result.stdout)
        # A repeatable, high loopback port keeps this project's browser layout
        # origin unchanged after a rename. Explicit --port 0 handles collisions.
        port=args.port if args.port is not None else 18000+int(setup['project']['id'][:8],16)%30000
        command=[sys.executable,str(engine),'observe',str(project),'--port',str(port)]
        if not args.no_open:command.append('--open')
        if args.prepare_only:
            print(json.dumps({'status':'prepared_not_started','project':setup['project'],'port':port,
                              'launch_command':command,'event_command':setup['event_command']},indent=2));return 0
        print('FORGE / '+setup['project']['name']+' — starting local Observatory',flush=True)
        os.execv(sys.executable,command)
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
        print('forge launcher: '+str(exc),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
