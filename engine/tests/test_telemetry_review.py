"""Independent regressions from real in-memory Observatory review findings.

No browser, listening socket or fabricated live-server claim is involved.
"""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from forge.cli import initialize
from forge.core import State
from forge.telemetry import emit, snapshot, serve


class TelemetryReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='forge-observatory-review-')
        self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name).resolve()/'project'
        initialize(self.project,'Review project','Check actual telemetry state')

    def event(self,kind,stamp,**kwargs):
        with patch('forge.core.time.time',return_value=stamp):
            emit(self.project,kind,**kwargs)

    def test_restarting_agent_id_resets_lifecycle_and_ownership(self):
        self.event('session.started',1000,agent_id='root',label='Session')
        self.event('agent.started',1001,agent_id='worker',parent_id='root',label='First task')
        self.event('agent.completed',1002,agent_id='worker',parent_id='root')
        self.event('agent.started',1003,agent_id='worker',parent_id='new-parent',label='Second task')
        state=snapshot(self.project,now=1004)
        worker=next(a for a in state['agents'] if a['id']=='worker')
        self.assertEqual(worker['status'],'running')
        self.assertEqual(worker['label'],'Second task')
        self.assertEqual(worker['parent_id'],'new-parent')
        self.assertEqual(worker['started_at'],1003)
        self.assertEqual(worker['updated_at'],1003)
        self.assertIsNone(worker['completed_at'])

    def test_single_idle_agent_does_not_complete_unrelated_work(self):
        self.event('session.started',1000,agent_id='root')
        self.event('agent.started',1001,agent_id='one',parent_id='root')
        self.event('agent.started',1002,agent_id='two',parent_id='root')
        self.event('idle',1003,agent_id='one',parent_id='root',label='Waiting')
        state=snapshot(self.project,now=1004)
        agents={a['id']:a for a in state['agents']}
        self.assertEqual(agents['one']['status'],'idle')
        for agent_id in ['root','two']:
            self.assertEqual(agents[agent_id]['status'],'running')
            self.assertIsNone(agents[agent_id]['completed_at'])
        self.assertEqual(state['status'],'running')

    def test_truncated_window_without_boundary_never_claims_confirmed_idle(self):
        self.event('session.started',1000,agent_id='root')
        self.event('agent.started',1001,agent_id='worker',parent_id='root')
        state=State(self.project/'.forge/state.sqlite')
        try:
            detail=json.dumps({'type':'check.passed','agent_id':'checker','parent_id':'root','label':'Recorded check','tokens':None})
            with state.db:
                state.db.executemany('INSERT INTO events(time,kind,detail) VALUES(?,?,?)',
                                     [(1002+i*.001,'telemetry',detail) for i in range(2001)])
        finally:state.close()
        result=snapshot(self.project,now=1005)
        self.assertEqual(result['status'],'stale')
        self.assertIn('incomplete',result['warning'].lower())
        self.assertEqual(result['stats']['events'],2003)
        for field in ['agents_started','checks_passed','checks_failed','elapsed_seconds','tokens']:
            self.assertIsNone(result['stats'][field],field)

    def test_visible_completion_does_not_invent_missing_session_totals(self):
        self.event('session.started',1000,agent_id='root')
        self.event('agent.started',1001,agent_id='worker',parent_id='root')
        state=State(self.project/'.forge/state.sqlite')
        try:
            detail=json.dumps({'type':'check.passed','agent_id':'checker','parent_id':'root','label':'Recorded check','tokens':None})
            with state.db:
                state.db.executemany('INSERT INTO events(time,kind,detail) VALUES(?,?,?)',
                                     [(1002+i*.001,'telemetry',detail) for i in range(2001)])
        finally:state.close()
        self.event('session.completed',1005,agent_id='root')
        result=snapshot(self.project,now=1006)
        self.assertEqual(result['status'],'idle')
        self.assertTrue(result['warning'])
        self.assertEqual(result['stats']['events'],2004)
        for field in ['agents_started','checks_passed','checks_failed','elapsed_seconds','tokens']:
            self.assertIsNone(result['stats'][field],field)

    def test_metadata_write_failure_closes_constructed_server_without_socket(self):
        class FakeServer:
            server_address=('127.0.0.1',8765)
            closed=False
            def server_close(self):self.closed=True
            def serve_forever(self,**kwargs):
                raise AssertionError('Metadata failure must stop before serving')
        server=FakeServer()
        with patch('forge.telemetry.ThreadingHTTPServer',return_value=server), \
             patch('forge.telemetry.atomic_json',side_effect=OSError('metadata write failed')):
            with self.assertRaisesRegex(OSError,'metadata write failed'):
                serve(self.project)
        self.assertTrue(server.closed)


if __name__=='__main__':unittest.main()
