"""Publish handoff reference; not production implementation or authentication."""
import pathlib
import unittest
import uuid
from service import Flow, Problem


class FlowChecks(unittest.TestCase):
    def setUp(self):
        self.flow = Flow(pathlib.Path(__file__).parent/'qa-runtime'/uuid.uuid4().hex[:8])

    def tearDown(self):
        self.flow.db.close()

    def command(self, action, item_id='p18', **args):
        item = self.flow.item(item_id)
        return self.flow.command({'request_id':str(uuid.uuid4()), 'action':action,
            'item':item_id, 'version':item['version'], 'args':args})

    def tick(self, item_id):
        self.flow.due[item_id] = 0
        self.flow.tick()

    def prepare(self, item_id):
        self.command('continue', item_id)
        self.tick(item_id)
        self.tick(item_id)

    def test_local_formal_version_and_push(self):
        f=self.flow; i=f.item('p18'); baseline=f.oid('refs/heads/master')
        initial=f.workflow_view(i)
        self.assertEqual(initial['current'],'prepare')
        self.assertFalse(any(s['state']=='passed' for s in initial['steps']))
        self.prepare('p18')
        self.assertEqual(i['check']['state'],'passed')
        self.command('compose',text=i['message'])
        self.assertEqual(f.oid('refs/heads/master'),baseline)
        self.command('plan',key='push',value=True)
        self.assertIn('push',[s['key'] for s in f.workflow_view(i)['steps']])
        self.command('continue'); self.tick('p18')
        self.assertEqual(i['phase'],'push')
        steps={s['key']:s for s in f.workflow_view(i)['steps']}
        self.assertEqual(steps['apply']['state'],'passed')
        self.assertEqual(steps['push']['state'],'ready')
        self.assertNotEqual(f.oid('refs/heads/master'),baseline)
        self.assertEqual(f.files(i['result']['oid'])['integrity.py'],f.files(i['source'])['integrity.py'])
        self.command('continue'); self.tick('p18')
        self.assertEqual(i['status'],'done')
        self.assertEqual(i['pushed']['remote_tip'],i['result']['oid'])
        self.assertTrue(all(s['state']=='passed' for s in f.workflow_view(i)['steps']))

    def test_failed_candidate_can_be_adopted_but_not_released(self):
        f=self.flow;i=f.item('p17');baseline=f.oid('refs/heads/master')
        self.prepare('p17')
        self.assertEqual(i['check']['state'],'failed')
        before_version=i['version']
        steps={s['key']:s for s in f.workflow_view(i)['steps']}
        self.assertEqual(steps['check']['state'],'failed')
        self.assertEqual(steps['route']['state'],'waiting')
        self.assertEqual(steps['apply']['state'],'pending')
        self.assertEqual(i['version'],before_version)
        with self.assertRaises(Problem): self.command('route','p17',choice='release')
        self.command('route','p17',choice='workspace')
        self.command('handoff','p17',text='先接手，空名称由 Lin 继续完善。')
        self.command('continue','p17');self.tick('p17')
        self.assertEqual(i['result']['role'],'workspace')
        self.assertEqual(f.target_tip(i),i['source'])
        self.assertEqual(f.oid('refs/heads/master'),baseline)
        steps={s['key']:s for s in f.workflow_view(i)['steps']}
        self.assertEqual(steps['check']['state'],'failed')
        self.assertEqual(steps['apply']['state'],'passed')

    def test_pause_revision_and_retry_identity(self):
        i=self.flow.item('p17');before=i['source']
        self.command('continue','p17');self.command('pause','p17');self.tick('p17')
        self.assertIsNone(i['evidence'])
        self.assertEqual(self.flow.workflow_view(i)['steps'][0]['state'],'paused')
        self.command('resume','p17');self.tick('p17');self.tick('p17')
        self.command('pause','p17');self.command('resume','p17')
        self.assertEqual(i['status'],'awaiting')
        self.command('revision','p17');self.prepare('p17')
        self.assertNotEqual(before,i['source']);self.assertEqual(i['check']['state'],'passed')
        payload={'request_id':str(uuid.uuid4()),'action':'note','item':'p17','version':i['version'],'args':{'text':'仅记录一次'}}
        a=self.flow.command(payload);b=self.flow.command(payload)
        self.assertEqual(a,b);self.assertEqual(len(i['notes']),1)
        self.command('route','p17',choice='discard')
        steps={s['key']:s for s in self.flow.workflow_view(i)['steps']}
        self.assertEqual(steps['apply']['state'],'skipped')
        self.assertEqual(steps['done']['state'],'passed')

    def test_stale_target_preserves_prepared_draft(self):
        f=self.flow;i=f.item('p18');self.prepare('p18');self.command('compose',text=i['message'])
        previous=f.oid('refs/heads/master');files=f.files(previous);files['new.txt']='external change'
        moved=f.make_commit(f.repo,files,[previous],'docs: concurrent target change')
        f.git('update-ref','refs/heads/master',moved,previous)
        self.command('continue');self.tick('p18')
        self.assertEqual(i['status'],'blocked');self.assertIsNone(i['result'])
        self.assertEqual(f.oid('refs/heads/master'),moved)
        self.assertIsNotNone(i['formal_id'])


if __name__=='__main__': unittest.main(verbosity=2)
