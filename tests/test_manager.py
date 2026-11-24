from contextlib import contextmanager, nullcontext, redirect_stdout
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class Contract(unittest.TestCase):
    def manager(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'manager.py'
        self.assertTrue(path.is_file(), 'The project manager CLI is missing')
        import manager
        return manager

    def load_example(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Path(tmp)/'config.yaml'
            cfg.touch(mode=0o600)
            cfg.write_bytes((m.ROOT/'examples'/'project.yaml').read_bytes())
            return m.load_config(cfg)

    def test_same_repository_name_does_not_authorize_a_replacement_id(self):
        self.manager()
        gh=importlib.import_module('github_io')
        auth=gh.Identity('org/project',{'type':'gh','login':'owner'},'hermes/test')
        auth.repo_id=123
        record={'id':456,'full_name':'org/project','clone_url':'https://github.com/org/project.git'}
        with patch.object(auth,'api',return_value=record):
            with self.assertRaisesRegex(gh.Blocked,'github-repo-scope-mismatch'):
                auth.canonical_repo()
            record['id']=123
            self.assertEqual(auth.canonical_repo(),record)

    def test_canonical_repo_rejects_drifted_clone_endpoint(self):
        self.manager()
        gh=importlib.import_module('github_io')
        auth=gh.Identity('org/project',{'type':'gh','login':'owner'},'hermes/test')
        auth.repo_id=123
        with patch.object(auth,'api',return_value={'id':123,'full_name':'org/project',
                          'clone_url':'https://github.com/other/project.git'}):
            with self.assertRaisesRegex(gh.Blocked,'github-clone-endpoint-mismatch'):
                auth.canonical_repo()

    def test_transferred_issue_cannot_supply_comments(self):
        self.manager()
        gh=importlib.import_module('github_io')
        auth=gh.Identity('org/project',{'type':'gh','login':'owner'},'hermes/test')
        auth.repo_id=123
        other={'id':91,'number':7,'repository_url':'https://api.github.com/repos/org/other',
               'url':'https://api.github.com/repos/org/other/issues/7'}
        with patch.object(auth,'canonical_repo'), patch.object(auth,'api',return_value=other), patch.object(auth,'gh') as comments:
            with self.assertRaisesRegex(gh.Blocked,'issue-routing-unknown'):
                auth.issue(7)
            comments.assert_not_called()

    def test_dashboard_labels_route_to_exactly_one_manager(self):
        m = self.manager()
        self.assertTrue(m.routed(['bug', 'hermes:alpha'], 'alpha'))
        for labels in ([], ['hermes:ALPHA'], ['hermes:alpha', 'hermes:beta'], ['hermes:beta']):
            self.assertFalse(m.routed(labels, 'alpha'))

    def test_bound_assignment_rejects_replacement_issue_at_same_number(self):
        m=self.manager()
        cfg={'dashboard':{'repo':'org/console','repo_id':11},
             'repositories':{'org/project':{'repo_id':22}}}
        key='org/console#7'
        row={'project':'org/project','issue_binding':{'source_repo_id':11,'target_repo_id':22,'issue_id':33}}
        with patch.object(m,'verified_issue',return_value=({'id':44},[])) as fetch:
            with self.assertRaisesRegex(m.Blocked,'issue-binding-changed'):
                m.bound_issue(cfg,key,row)
            fetch.return_value=({'id':33},[])
            self.assertEqual(m.bound_issue(cfg,key,row),({'id':33},[]))
            cfg['repositories']['org/project']['repo_id']=55
            with self.assertRaisesRegex(m.Blocked,'issue-binding-changed'):
                m.bound_issue(cfg,key,row)

    def test_configured_repository_id_reaches_transport(self):
        m=self.manager()
        cfg=self.load_example()
        repo=cfg['dashboard']['repo']
        cfg['dashboard']['repo_id']=17
        auth=m.identity(cfg,repo)
        with patch.object(auth,'api',return_value={'id':17,'full_name':repo,'clone_url':f'https://github.com/{repo}.git'}):
            self.assertEqual(auth.canonical_repo()['id'],17)

    def test_config_separates_console_identity_and_repository_authority(self):
        m = self.manager()
        cfg = self.load_example()
        self.assertEqual(cfg['dashboard']['repo'], 'example-org/manager-dashboard')
        self.assertEqual(cfg['repositories']['example-org/external-project']['kind'], 'external')
        self.assertEqual(sum(x['kind'] == 'owned' for x in cfg['repositories'].values()), 3)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'bad.yaml'
            config.touch(mode=0o600)
            config.write_text('dashboard: {}\ndashboard: {}\n')
            with self.assertRaisesRegex(m.Blocked,'duplicate-or-nontext-yaml-key'):
                m.load_config(config)

    def test_quota_planning_uses_agent_manager_advice_without_recalculating(self):
        m = self.manager()
        adapter = object.__new__(m.Adapter)
        adapter.cfg = {'worker':{'agent':'codex','quota_pane':'w:p1'}}
        with patch.object(adapter,'run',return_value={'hold':None,'usable_windows':['secondary'],'behind_windows':[]}) as run:
            self.assertIsNone(adapter.quota()['hold'])
            run.assert_called_once_with(m.AGENT,'check-quota','--agent','codex','--pane','w:p1')
            run.return_value.update(hold='quota_behind_pace', behind_windows=['secondary'])
            self.assertEqual(adapter.quota()['hold'],'quota_behind_pace')
            run.return_value={'primary':{},'secondary':None}
            with self.assertRaises(m.Blocked):
                adapter.quota()

    def test_config_loader_does_not_follow_symlink(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config.yaml'
            config.symlink_to(m.ROOT/'examples'/'project.yaml')
            with self.assertRaisesRegex(m.Blocked,'cannot-read-safe-yaml'):
                m.load_config(config)

    def test_adapter_cannot_execute_dependency_from_symlinked_skill_root(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp); skills=home/'skills'; skills.mkdir()
            for name,script in m.SKILLS.items():
                real=home/(name+'-elsewhere')
                (real/'scripts').mkdir(parents=True)
                (real/'SKILL.md').write_text('fixture')
                (real/'scripts'/script).write_text('raise SystemExit(100)')
                (skills/name).symlink_to(real,target_is_directory=True)
            with patch.object(m,'home',return_value=home):
                with self.assertRaisesRegex(m.Blocked,'unverified-skill-root'):
                    m.Adapter({'herdr':{'instance':'default','workspace':'fixture'}})

    def test_follow_up_leaves_worker_waiting_after_agent_quota_refusal(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp, m.State(Path(tmp),'fixture') as state:
            cfg={'herdr':{'instance':'fixture','workspace':'w'},'worker':{'quota_pane':'w:probe','network':False,'max_active':1},'health':{}}
            key='org/repo#1'
            state.data['items'][key]={'project':'org/repo','state':'waiting','scope':['fixture','w'],
                                      'pane':'w:p1','thread':'thread','turn':'last','worker_network':False}
            with (patch.object(m,'Adapter') as cls, patch.object(m,'bound_issue'),
                  patch.object(m,'check_health',return_value={'blocks':[]})):
                adapter=cls.return_value
                adapter.scope=('fixture','w')
                def steer(*args,before_send):
                    before_send()
                    raise m.Blocked('quota_behind_pace')
                adapter.steer.side_effect=steer
                with self.assertRaisesRegex(m.Blocked,'quota_behind_pace'):
                    m.follow_up(cfg,state,key,'Continue',lambda:None)
                adapter.quota.assert_not_called()
            row=m.load_private(state.path)['items'][key]
            self.assertEqual((row['state'],row['turn']),('waiting','last'))

    def test_follow_up_tab_lock_preserves_idle_turn(self):
        m=self.manager()
        cfg={'herdr':{'instance':'fixture','workspace':'w'},'worker':{'quota_pane':'w:probe','network':False,'max_active':1},'health':{}}
        with tempfile.TemporaryDirectory() as tmp, m.State(Path(tmp),'fixture') as state:
            key='org/repo#1'
            row=state.data['items'][key]={'project':'org/repo','state':'waiting','scope':['fixture','w'],
                                         'pane':'w:p1','thread':'thread','turn':'last','worker_network':False}
            with (patch.object(m,'Adapter') as cls, patch.object(m,'bound_issue'),
                  patch.object(m,'check_health',return_value={'blocks':[]})):
                agent=cls.return_value; agent.scope=('fixture','w')
                def steer(*args,before_send):
                    before_send()
                    raise m.Blocked('tab_user_locked')
                agent.steer.side_effect=steer
                with self.assertRaisesRegex(m.Blocked,'tab_user_locked'):
                    m.follow_up(cfg,state,key,'Continue',lambda:None)
            self.assertEqual((row['state'],row['turn']),('waiting','last'))

    def test_initial_start_rechecks_guard_after_agent_help(self):
        m=self.manager()
        adapter=object.__new__(m.Adapter); adapter.cfg={'worker':{'agent':'codex','model':'fixture','network':False}}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp); entry=folder/'agent.py'; entry.touch(mode=0o600)
            adapter.paths={m.AGENT:entry}
            active=True
            def cli_help(*_):
                nonlocal active
                active=False
                return '--override-quota'
            def guard():
                if not active: raise m.Blocked('activation-changed-during-pass')
            with (patch.object(adapter,'entry',return_value=str(entry)), patch.object(m,'call',side_effect=cli_help),
                  patch.object(adapter,'run',return_value={'thread_id':'t','turn_id':'n','status':'accepted'}) as send):
                with self.assertRaisesRegex(m.Blocked,'activation-changed-during-pass'):
                    adapter.start(folder,'w:p1','private task',before_send=guard)
                send.assert_not_called()

    def test_initial_quota_refusal_keeps_receipt_without_closing_the_pane(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp, m.State(Path(tmp),'fixture') as state:
            key='org/repo#1'
            row=state.begin(key,'org/repo')
            row.update(pane='w:p1',scope=['fixture','w'],provision_attempted=True,checkout='existing-evidence')
            with patch.object(m,'Adapter') as cls:
                m.record_dispatch_hold(state,key,row,m.Blocked('quota_behind_pace'))
                cls.assert_not_called()
            stored=m.load_private(state.path)['items'][key]
            self.assertEqual(stored['state'],'uncertain')
            self.assertEqual(stored['pane'],'w:p1')
            self.assertEqual(stored['hold'],'quota_behind_pace')
            self.assertEqual(stored['checkout'],'existing-evidence')

    def test_provision_rejects_pane_not_new_in_exact_workspace_or_wrong_receiver(self):
        m=self.manager()
        adapter = object.__new__(m.Adapter)
        adapter.cfg = {'worker':{'agent':'codex'}}
        adapter.scope = ('fixture','w')
        with tempfile.TemporaryDirectory() as tmp:
            cwd=Path(tmp).resolve()
            make=lambda *ids: {'tabs':[{'panes':[{'pane_id':pane} for pane in ids]}]}
            ready=dict(status='ready',pane_id='w:new',agent='codex',cwd=str(cwd))
            for receipt, before, after in (
                (ready,make('w:new'),make('w:new')),
                (ready,make('w:old'),make('w:old')),
                (dict(ready,agent='hermes'),make('w:old'),make('w:old','w:new')),
                (dict(ready,cwd=str(cwd/'unrelated')),make('w:old'),make('w:old','w:new')),
            ):
                with patch.object(adapter,'layout',side_effect=[before,after]), patch.object(adapter,'run',return_value=receipt):
                    with self.assertRaisesRegex(m.Blocked,'provision-not-verified'):
                        adapter.provision(cwd,'fixture')
            with patch.object(adapter,'layout',side_effect=[make('w:old'),make('w:old','w:new')]), patch.object(adapter,'run',return_value=ready):
                self.assertEqual(adapter.provision(cwd,'fixture'),'w:new')

    def test_disk_pressure_after_checkout_or_provision_holds_and_keeps_evidence(self):
        m=self.manager()
        cfg={'health':{},'worker':{'model':'fixture-model','network':False},
             'dashboard':{'repo':'org/console'},'repositories':{'org/repo':{'repo_id':17,'kind':'owned'}}}
        key='org/repo#1'
        for after_provision in (False,True):
            with tempfile.TemporaryDirectory() as tmp, m.State(Path(tmp),'fixture') as state:
                row=state.begin(key,'org/repo')
                folder=Path(tmp)/'worker-checkout'; folder.mkdir()
                healthy=dict(blocks=[])
                pressure=dict(blocks=['low-disk-space'])
                health=[healthy,healthy,pressure] if after_provision else [healthy,pressure]
                with (patch.object(m,'Adapter') as cls, patch.object(m,'verified_issue',return_value=({'id':31,'title':'Fixture','body':''},[])),
                      patch.object(m,'prepare_checkout',return_value=(folder,'abc')), patch.object(m,'check_health',side_effect=health) as check):
                    adapter=cls.return_value
                    adapter.scope=('fixture','w')
                    adapter.provision.return_value='w:p1'
                    adapter.start.return_value={'thread_id':'t','turn_id':'n'}
                    with self.assertRaisesRegex(m.Blocked,'host-pressure-before-dispatch') as caught:
                        m.assignment(cfg,state,adapter,key,row,lambda:None)
                    m.record_dispatch_hold(state,key,row,caught.exception)
                    self.assertEqual(check.call_args.args[0],folder)
                    self.assertEqual(adapter.provision.call_count,int(after_provision))
                    adapter.start.assert_not_called()
                stored=m.load_private(state.path)['items'][key]
                self.assertEqual(stored['checkout'],folder.name)
                self.assertNotEqual(stored['state'],'queued')

    def test_state_survives_reload_and_ambiguous_dispatch_is_not_replayed(self):
        m = self.manager()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with m.State(root, 'alpha') as s:
                item = s.begin('x/y#20', 'x/y')
                self.assertEqual(item['state'], 'starting')
                with self.assertRaises(m.Blocked):
                    s.begin('x/y#20', 'x/y')
            with m.State(root, 'alpha') as s:
                self.assertEqual(s.data['items']['x/y#20']['state'], 'starting')
                with self.assertRaises(m.Blocked):
                    s.begin('x/y#20', 'x/y')
            with self.assertRaises(m.Blocked):
                with m.State(root, 'other'):
                    pass

    def test_dashboard_joins_geometry_only_to_owned_panes(self):
        m = self.manager()
        view = {'schema_version': 1, 'instance':'session', 'workspace_id':'w', 'tabs':[
            {'panes':[{'pane_id':'known','label':'secret <path>','rect':{'x':1,'y':2,'width':30,'height':10},'user_locked':True},
                      {'pane_id':'unknown','label':'unrelated','rect':None}]}]}
        items = {'owner/repo#1':{'pane':'known','state':'running','project':'owner/repo'}}
        facts = m.layout_facts(view, items, scope=('session','w'))
        self.assertEqual(facts, [[{'issue':'owner/repo#1','rect':[1,2,30,10],'locked':True,'state':'running'}]])
        self.assertNotIn('secret', json.dumps(facts))
        self.assertEqual(m.layout_facts(view,items,scope=('wrong','w')), [])

    def test_real_git_wiki_writes_only_owned_page(self):
        m = self.manager()
        gh = importlib.import_module('github_io')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bare = tmp / 'wiki.git'
            seed = tmp / 'seed'
            subprocess.run(['git','init','-q','--bare','--initial-branch=main',str(bare)],check=True)
            subprocess.run(['git','clone','-q',str(bare),str(seed)],check=True,capture_output=True)
            def git(*args):
                return subprocess.check_output(['git','-C',str(seed),'-c','user.name=tests','-c','user.email=tests@example.invalid',*args],text=True)
            (seed/'Home.md').write_text('# unchanged\n')
            git('add','Home.md'); git('commit','-qm','seed'); git('push','-q','origin','HEAD:main')
            identity = lambda args, **kw: gh.call(['git','-c','user.name=tests','-c','user.email=tests@example.invalid',*args],**kw)
            gh.publish_wiki(identity, str(bare), 'Manager-alpha.md', '# Alpha\n', tmp)
            gh.publish_wiki(identity, str(bare), 'Manager-beta.md', '# Beta\n', tmp)
            self.assertEqual(gh.publish_wiki(identity, str(bare), 'Manager-alpha.md', '# Alpha\n', tmp)['status'],'unchanged')
            git('pull','-q','--ff-only')
            self.assertEqual((seed/'Home.md').read_text(),'# unchanged\n')
            self.assertEqual((seed/'Manager-alpha.md').read_text(),'# Alpha\n')
            self.assertEqual((seed/'Manager-beta.md').read_text(),'# Beta\n')

    def test_renderer_does_not_include_private_inputs(self):
        m = self.manager()
        row={'state':'waiting','project':'org/repo','pane':'a','answer':'private secret','cwd':'/private/path'}
        text=m.dashboard({'org/repo#7':row}, [], ['quota-unknown'], '2026-11-22T20:00:00Z', [], 'hermes/example')
        self.assertIn('https://github.com/org/repo/issues/7', text)
        self.assertNotIn('private',text)

    def test_dispatch_crash_window_remains_unknown(self):
        m = self.manager()
        with tempfile.TemporaryDirectory() as tmp:
            with m.State(Path(tmp),'alpha') as s:
                s.begin('x/y#1','x/y')
                m.reconcile({},s,Mock())
                self.assertEqual(s.data['items']['x/y#1']['state'],'uncertain')

    def test_cleanup_keeps_active_evidence_and_ignores_symlinks(self):
        import os
        m = self.manager()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            scratch=m.private(root/'scratch')
            expired=scratch/('report-'+'a'*32+'.done'); expired.write_text('expired')
            protected=scratch/('report-'+'b'*32+'.done'); protected.write_text('review evidence')
            unrelated=root/'keep'; unrelated.write_text('safe')
            suspicious=scratch/('report-'+'c'*32+'.done'); suspicious.symlink_to(unrelated)
            for f in (expired,protected): os.utime(f,(100,100))
            self.assertEqual(m.clean_expired(root,1,10000,protect={protected.name}),1)
            self.assertTrue(protected.exists() and suspicious.is_symlink() and unrelated.exists())

    def test_watchdog_live_lock_never_calls_layout_or_dispatch(self):
        m=self.manager()
        cfg={'herdr':{'instance':'s','workspace':'w'}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with m.lock(root,'manager.live.lock'), patch.object(m,'Adapter') as adapter:
                receipt=m.watchdog(cfg,root,root/'config.yaml',False)
                adapter.return_value.layout.assert_not_called()
            self.assertEqual(receipt,{'status':'manager-health-unverified','work_dispatched':False})

    def test_unarmed_watchdog_refuses_even_to_construct_adapter(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp, patch.object(m,'Adapter') as adapter:
            root=Path(tmp)
            with self.assertRaisesRegex(m.Blocked,'verified-preflight'):
                m.watchdog({},root,root/'config.yaml',True)
            adapter.assert_not_called()

    def test_scoped_app_git_uses_the_launcher_without_configuration_overrides(self):
        self.manager()
        gh = importlib.import_module('github_io')
        identity=gh.Identity('org/repo',{'type':'app','login':'test[bot]','launcher':'approved-app'},'hermes/test')
        identity.verified=True
        with patch.object(gh,'call',return_value='receipt') as invoke:
            self.assertEqual(identity.git(['ls-remote','https://github.com/org/repo.wiki.git','HEAD']),'receipt')
        invoke.assert_called_once_with(['approved-app','--repo','org/repo','git','ls-remote','https://github.com/org/repo.wiki.git','HEAD'])

    def test_existing_pr_does_not_erase_an_ambiguous_worker_attempt(self):
        m = self.manager()
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'identity') as auth, patch.object(m,'verify_saved_issue'):
            with m.State(Path(tmp), 'alpha') as state:
                item = state.begin('org/console#1', 'org/project')
                item['pr'] = 4
                auth.return_value.pull_state.return_value = {'state': 'working', 'head': 'a' * 40,
                                                                'checks': 'pass', 'draft': False}
                m.reconcile({}, state, Mock())
                self.assertEqual(item['state'], 'uncertain')
                self.assertEqual(item['checks'], 'pass')

    def test_skipped_check_is_not_proof_of_ready_to_review(self):
        self.manager()
        gh = importlib.import_module('github_io')
        auth = gh.Identity('org/repo', {'type': 'gh', 'login': 'owner'}, 'hermes/test')
        head = 'a' * 40
        checks = [{'head_sha': head, 'status': 'completed', 'conclusion': result}
                  for result in ('success', 'skipped')]
        with patch.object(auth, 'api', side_effect=[{'number': 7, 'head': {'sha': head},
                                                      'state': 'open', 'draft': False},
                                                     {'sha': head, 'total_count': 0}]), \
             patch.object(auth, 'gh', return_value='\n'.join(map(json.dumps, checks))):
            self.assertEqual(auth.pull_state(7)['checks'], 'pending')

    def test_configured_check_name_requires_the_pinned_workflow(self):
        self.manager()
        gh=importlib.import_module('github_io')
        auth=gh.Identity('org/project',{'type':'gh','login':'owner'},'hermes/test',repo_id=22)
        head='a'*40; path='.github/workflows/ci.yml'; blob='b'*40
        policy={'tests':{'workflow_id':11,'path':path,'blob':blob}}
        check={'id':18,'name':'tests','head_sha':head,'status':'completed','conclusion':'success','suite_id':17,'app_slug':'github-actions'}
        run={'id':30,'run_number':10,'run_attempt':2,'workflow_id':99,'path':path,'head_sha':head,'check_suite_id':17,'event':'push',
             'head_branch':'main','repository':{'id':22},'head_repository':{'id':22},'status':'completed','conclusion':'success'}
        runs=[run]
        jobs={'total_count':1,'jobs':[{'run_id':30,'run_attempt':1,'name':'tests','head_sha':head,'head_branch':'main',
             'status':'completed','conclusion':'success','check_run_url':'https://api.github.com/repos/org/project/check-runs/18'}]}
        def api(endpoint):
            if '/status' in endpoint: return {'sha':head,'total_count':0}
            if '/contents/' in endpoint: return {'sha':blob,'type':'file','path':path}
            if '/attempts/2/jobs?' in endpoint or '/attempts/3/jobs?' in endpoint: return jobs
            if '/actions/' in endpoint: return {'total_count':len(runs),'workflow_runs':runs}
            return {'number':7,'head':{'sha':head},'state':'open','draft':False}
        checks=[check]
        with patch.object(auth,'api',side_effect=api),patch.object(auth,'gh',side_effect=lambda *_:'\n'.join(map(json.dumps,checks))):
            self.assertNotEqual(auth.pull_state(7,required=policy)['checks'],'pass')
            run['workflow_id']=11
            self.assertNotEqual(auth.pull_state(7,required=policy)['checks'],'pass')
            jobs['jobs'][0]['run_attempt']=2
            self.assertEqual(auth.pull_state(7,required=policy)['checks'],'pass')
            runs.append(dict(run,id=31,run_number=11,check_suite_id=19,status='queued',conclusion=None))
            self.assertNotEqual(auth.pull_state(7,required=policy)['checks'],'pass')
            runs[-1].update(status='completed',conclusion='success',run_attempt=3)
            checks.append(dict(check,id=20,suite_id=19))
            jobs['jobs'][0].update(run_id=31,check_run_url='https://api.github.com/repos/Org/Project/check-runs/20')
            del jobs['jobs'][0]['run_attempt']
            self.assertEqual(auth.pull_state(7,required=policy)['checks'],'pass')
            runs.pop()
            checks.pop()
            jobs['jobs'][0].update(run_id=30,run_attempt=2,check_run_url='https://api.github.com/repos/org/project/check-runs/18')
            jobs['jobs'][0]['check_run_url']=jobs['jobs'][0]['check_run_url'].replace('/18','/19')
            self.assertNotEqual(auth.pull_state(7,required=policy)['checks'],'pass')
            jobs['jobs'][0]['check_run_url']=jobs['jobs'][0]['check_run_url'].replace('/19','/18')
            run['event']='pull_request'
            self.assertNotEqual(auth.pull_state(7,required=policy)['checks'],'pass')

    def test_exact_head_approval_from_unpinned_actor_is_not_independent(self):
        m=self.manager()
        auth=Mock(repo='org/project',reviewer_ids=[81]); auth.spec={'login':'publisher[bot]'}; head='a'*40
        pr={'number':7,'head':{'sha':head},'user':{'login':'publisher[bot]','id':92}}
        review={'id':11,'state':'APPROVED','commit_id':head,'author_association':'COLLABORATOR',
                'user':{'id':82,'login':'reviewer'},'pull_request_url':'https://api.github.com/repos/org/project/pulls/7'}
        auth.api.side_effect=[pr,review]
        auth.gh.return_value=json.dumps({'id':11,'login':'reviewer','user_id':82,'state':'APPROVED','commit_id':head})
        with self.assertRaisesRegex(m.Blocked,'independent-exact-head-review-not-verified'):
            m.verify_review(auth,7,head,11)

    def test_followup_without_atomic_thread_binding_does_not_send(self):
        m = self.manager()
        adapter = object.__new__(m.Adapter)
        with patch.object(adapter, 'run') as run:
            with self.assertRaisesRegex(m.Blocked, 'thread-binding'):
                adapter.steer('pane', 'private prompt', None, None)
            run.assert_not_called()

    def test_wiki_never_copies_restricted_metadata_into_a_different_repository(self):
        m = self.manager()
        cfg: dict = {'dashboard': {'repo': 'org/dash'}}
        meta = {'org/dash': {'private': True,'visibility':'private'},
                'org/private': {'private': True,'visibility':'private'},
                'org/public': {'private': False,'visibility':'public'}}
        self.assertFalse(m.wiki_allows(cfg, meta, 'org/dash', 'org/private', 'org/private'))
        self.assertTrue(m.wiki_allows(cfg, meta, 'org/dash', 'org/dash', 'org/public'))
        self.assertTrue(m.wiki_allows(cfg, meta, 'org/private', 'org/private', 'org/private'))
        cfg['dashboard']['private_sources'] = ['org/private']
        self.assertFalse(m.wiki_allows(cfg, meta, 'org/dash', 'org/private', 'org/private'))

    def test_cross_repository_publication_without_exact_consent_never_reads_bundle(self):
        m=self.manager()
        key='org/console#7'; project='org/project'; head='a'*40
        cfg={'name':'fixture','dashboard':{'repo':'org/console','repo_id':11},'repositories':{project:{'repo_id':22,'base':'main'}}}
        with tempfile.TemporaryDirectory() as tmp, m.State(Path(tmp),'fixture') as state:
            state.data['items'][key]={'project':project,'state':'waiting','checkout':'b'*16,
                'issue_binding':{'source_repo_id':11,'target_repo_id':22,'issue_id':29}}
            source=Mock(); source.canonical_repo.return_value={'id':11,'private':True,'visibility':'private'}
            target=Mock(); target.canonical_repo.return_value={'id':22,'private':False,'visibility':'public'}
            with (patch.object(m,'identity',side_effect=lambda _,repo:{'org/console':source,project:target}[repo]),
                  patch.object(m,'bound_issue',return_value=({'id':29},[])),
                  patch.object(m,'import_bundle') as bundle):
                row=state.data['items'][key]
                with self.assertRaisesRegex(m.Blocked,'operator-publication-approval-required'):
                    m.publish_item(cfg,state,key,head,lambda:None)
                row.update(state='uncertain',publish_sha=head,publish_branch='hermes/fixture/'+'b'*16,
                           pending_disclosure={'authorization':'legacy-digest'})
                with self.assertRaisesRegex(m.Blocked,'operator-publication-approval-required'):
                    m.publish_item(cfg,state,key,head,lambda:None)
                target.canonical_repo.return_value={'id':22,'private':True,'visibility':'private'}
                with self.assertRaisesRegex(m.Blocked,'publication-target-audience-unverified'):
                    m.publish_item(cfg,state,key,head,lambda:None,approval={'message':'x','signature':'y'})
                bundle.assert_not_called()
                target.git.assert_not_called()
                target.gh.assert_not_called()

    def test_pr_text_does_not_link_cross_repository_source(self):
        self.manager()
        gh = importlib.import_module('github_io')
        auth = gh.Identity('org/project', {'type': 'gh', 'login': 'owner'}, 'hermes/test')
        title, body = auth.pr_text('org/private-dash#12', 'a' * 40)
        self.assertNotIn('private-dash', title + body)
        self.assertNotIn('#12', title + body)
        self.assertIn('#12', auth.pr_text('org/project#12', 'a' * 40)[1])

    def test_operator_signature_required_even_for_same_repository(self):
        m=self.manager()
        project='org/project'; key=f'{project}#7'; head='a'*40
        cfg={'repositories':{project:{}},'name':'fixture'}
        with tempfile.TemporaryDirectory() as tmp,m.State(Path(tmp),'fixture') as state:
            state.data['items'][key]={'project':project,'state':'waiting','checkout':'b'*16}
            with patch.object(m,'bound_issue'),patch.object(m,'import_bundle') as bundle:
                with self.assertRaisesRegex(m.Blocked,'operator-publication-approval-required'):
                    m.publish_item(cfg,state,key,head,lambda:None)
                bundle.assert_not_called()

    def test_signature_cannot_authorize_a_different_commit_or_a_later_publication(self):
        m=self.manager()
        scope={'head':'a'*40,'target':'org/project','target_visibility':'public'}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); approved=root/'approved'; unknown=root/'unknown'; now=1735689600
            for private_key in (approved,unknown):
                m.call(['/usr/bin/ssh-keygen','-q','-t','ed25519','-N','','-f',str(private_key)])
            public_key=(root/'approved.pub').read_text().strip().split()[:2]
            message=m.approval_message(scope,now=now)
            def sign(private_key):
                return m.call(['/usr/bin/ssh-keygen','-Y','sign','-f',str(private_key),'-n',m.APPROVAL_NAMESPACE],data=message)
            signed=sign(approved); stranger=sign(unknown)
            with patch.object(m,'operator_public_key',return_value=' '.join(public_key)):
                m.verify_publication_approval(root,scope,{'message':message,'signature':signed},now=now)
                with self.assertRaisesRegex(m.Blocked,'invalid-operator-publication-signature'):
                    m.verify_publication_approval(root,scope,{'message':message,'signature':stranger},now=now)
                with self.assertRaisesRegex(m.Blocked,'publication-approval-scope-changed'):
                    m.verify_publication_approval(root,dict(scope,head='b'*40),{'message':message,'signature':signed},now=now)
                with self.assertRaisesRegex(m.Blocked,'publication-approval-expired'):
                    m.verify_publication_approval(root,scope,{'message':message,'signature':signed},now=now+3600)

    def test_recovering_advance_distinguishes_unchanged_prior_head(self):
        m=self.manager()
        project='org/project'; key=f'{project}#7'; prior='b'*40; expected='c'*40; checkout='d'*16
        branch='hermes/fixture/1234567890123456'; ref='refs/heads/'+branch
        cfg={'repositories':{project:{'base':'main'}},'name':'fixture'}
        with tempfile.TemporaryDirectory() as tmp, m.State(Path(tmp),'fixture') as state:
            (state.root/'work'/checkout).mkdir(parents=True)
            row=state.data['items'][key]={'project':project,'state':'uncertain','checkout':checkout,
                'published_sha':prior,'publish_sha':expected,'publish_branch':branch,'pr':4}
            auth=Mock(); auth.git.return_value=f'{prior}\t{ref}\n'
            def lookup(_,__,head,**kwargs):
                if head!=prior: raise m.Blocked('pr-actor-or-head-mismatch')
                self.assertTrue(kwargs['require_open'])
                return 4
            auth.existing_pr.side_effect=lookup
            with (patch.object(m,'identity',return_value=auth), patch.object(m,'publication_scope',return_value={'base_sha':'a'*40}),
                  patch.object(m,'verify_publication_approval'),
                  patch.object(m,'import_bundle',side_effect=m.Blocked('resume-reached-bundle')) as bundle,
                  patch.object(m,'call',return_value='a'*40)):
                with self.assertRaisesRegex(m.Blocked,'resume-reached-bundle'):
                    m.publish_item(cfg,state,key,expected,lambda:None,approval={'message':'x','signature':'y'})
                bundle.assert_called_once()
                auth.push_pr.assert_not_called()
                self.assertEqual(row['published_sha'],prior)

    def test_replaced_repository_cannot_reconcile_saved_pull_request(self):
        m=self.manager()
        project='org/project'; key=f'{project}#7'; head='a'*40
        cfg={'dashboard':{'repo':'org/console','repo_id':10},'repositories':{project:{'repo_id':21,'base':'main','kind':'owned'}}}
        with tempfile.TemporaryDirectory() as tmp,m.State(Path(tmp),'fixture') as state:
            row=state.data['items'][key]={'project':project,'state':'human','pr':4,'checks':'pass',
                'published_sha':head,'issue_binding':{'source_repo_id':20,'target_repo_id':20,'issue_id':33}}
            with patch.object(m,'identity') as identity:
                identity.return_value.pull_state.return_value={'head':head,'state':'working','checks':'pass','draft':False}
                m.reconcile(cfg,state,Mock())
                identity.assert_not_called()
            self.assertEqual(row['hold'],'issue-binding-changed')
            self.assertNotIn('checks',row)
            self.assertNotEqual(row['state'],'human')

    def test_verified_push_can_resume_pr_creation_without_worker_checkout(self):
        m=self.manager()
        project='org/project'; key=f'{project}#7'; branch='hermes/fixture/'+'b'*16
        head='a'*40; base='c'*40; ref=f'refs/heads/{branch}'
        cfg={'name':'fixture','repositories':{project:{'base':'main'}}}
        proof={'message':'x','signature':'y'}
        with tempfile.TemporaryDirectory() as tmp,m.State(Path(tmp),'fixture') as state:
            row=state.data['items'][key]={'project':project,'state':'uncertain','checkout':'d'*16,
                                         'publish_branch':branch,'publish_sha':head,'pending_approval':proof}
            auth=Mock()
            auth.git.return_value=f'{head}\t{ref}'
            auth.existing_pr.return_value=None
            auth.push_pr.return_value=4
            with (patch.object(m,'identity',return_value=auth),
                  patch.object(m,'publication_scope',return_value={'base_sha':base,'branch':branch}),
                  patch.object(m,'verify_publication_approval') as verify,patch.object(m,'import_bundle') as bundle):
                with self.assertRaisesRegex(m.Blocked,'recovery-approval-required'):
                    m.publish_item(cfg,state,key,head,lambda:None)
                auth.push_pr.assert_not_called()
                row['push_confirmed']=True
                self.assertEqual(m.publish_item(cfg,state,key,head,lambda:None)['pr'],4)
            self.assertIsNone(auth.push_pr.call_args.args[0])
            self.assertTrue(auth.push_pr.call_args.kwargs['resume'])
            verify.assert_called()
            bundle.assert_not_called()

    def test_confirmed_push_does_not_adopt_unexpected_pr(self):
        m=self.manager(); project='org/project'; key=f'{project}#7'
        head='a'*40; branch='hermes/fixture/'+'b'*16
        cfg={'name':'fixture','repositories':{project:{'base':'main'}}}
        with tempfile.TemporaryDirectory() as tmp,m.State(Path(tmp),'fixture') as state:
            row=state.data['items'][key]={'project':project,'state':'uncertain','checkout':'c'*16,
                 'publish_branch':branch,'publish_sha':head,'push_confirmed':True,
                 'pending_approval':{'message':'old','signature':'old'}}
            auth=Mock(); auth.git.return_value=f'{head}\trefs/heads/{branch}'; auth.existing_pr.return_value=17
            scope={'base_sha':'d'*40,'recovery_pr':17,'title':'Example','body':'Review this.'}
            with (patch.object(m,'identity',return_value=auth),
                  patch.object(m,'publication_scope',return_value=scope),patch.object(m,'verify_publication_approval')):
                with self.assertRaisesRegex(m.Blocked,'recovery-approval-required'):
                    m.publish_item(cfg,state,key,head,lambda:None)
                self.assertNotIn('pr',row)
                proof={'message':'new','signature':'new'}
                self.assertEqual(m.publish_item(cfg,state,key,head,lambda:None,approval=proof,recover=True)['pr'],17)
            self.assertEqual(row['publication_approval_sha256'],m.hashlib.sha256(b'new').hexdigest())
            auth.push_pr.assert_not_called()

    def test_advance_requires_the_signed_pr_number(self):
        self.manager()
        gh=importlib.import_module('github_io')
        auth=gh.Identity('org/project',{'type':'gh','login':'fixture'},'hermes/fixture')
        branch='hermes/fixture/'+'a'*16; prior='b'*40; head='c'*40
        def fake_git(argv,**kwargs):
            return prior if 'merge-base' in argv else f'{prior}\trefs/heads/{branch}'
        with (patch.object(auth,'check_pr_target'),patch.object(auth,'verify'),
              patch.object(auth,'git',side_effect=fake_git) as git,
              patch.object(auth,'existing_pr',side_effect=[18,19])):
            with self.assertRaisesRegex(gh.Blocked,'previous-publication-not-verified'):
                auth.push_pr(Path('/fixture'),branch,'main','org/project#7',head,prior,previous_pr=18,
                             before_write=lambda:None,final_guard=lambda:None,resume=False,
                             before_push=lambda:None,after_push=lambda:None)
            self.assertFalse(any('push' in c.args[0] for c in git.call_args_list))

    def test_ref_conflict_before_push_does_not_create_recovery_authority(self):
        m=self.manager()
        key='org/project#7'; project='org/project'; checkout='b'*16
        head='a'*40; base='c'*40
        cfg={'name':'fixture','repositories':{project:{'base':'main'}}}
        with tempfile.TemporaryDirectory() as tmp,m.State(Path(tmp),'fixture') as state:
            (state.root/'work'/checkout).mkdir(parents=True)
            row=state.data['items'][key]={'state':'waiting','project':project,'checkout':checkout,'base_sha':base}
            auth=Mock()
            auth.git.return_value=f'{base}\trefs/heads/main'
            auth.push_pr.side_effect=m.Blocked('remote-branch-conflict')
            with (patch.object(m,'identity',return_value=auth),patch.object(m,'call',return_value=base),
                  patch.object(m,'publication_scope',return_value={'base_sha':base}),
                  patch.object(m,'verify_publication_approval'),patch.object(m,'import_bundle')):
                with self.assertRaisesRegex(m.Blocked,'remote-branch-conflict'):
                    m.publish_item(cfg,state,key,head,lambda:None,approval={'message':'x','signature':'y'})
            self.assertEqual(row['state'],'waiting')
            self.assertNotIn('publish_sha',row)

    def test_replaced_repository_cannot_publish_saved_wiki_row_or_log(self):
        m=self.manager()
        project='org/project'; key=f'{project}#7'; dash='org/console'
        cfg={'name':'fixture','publication':{'signature':'hermes/fixture'},'dashboard':{'repo':dash,'repo_id':10},
             'repositories':{project:{'repo_id':21,'kind':'owned'}}}
        metadata={repo:{'id':id_,'has_wiki':True,'private':False,'visibility':'public'} for repo,id_ in ((project,21),(dash,10))}
        with tempfile.TemporaryDirectory() as tmp,m.State(Path(tmp),'fixture') as state:
            items={key:{'project':project,'state':'queued',
                        'issue_binding':{'source_repo_id':20,'target_repo_id':20,'issue_id':33}}}
            state.data['events']=[{'kind':'started','at':'2026-01-01T00:00:00Z','item':key}]
            with patch.object(m,'identity'),patch.object(m,'publish_wiki',return_value={'status':'published'}) as write:
                m.wiki_pages(cfg,state,items,[],[],[],metadata,lambda:None)
                self.assertEqual(len(write.call_args_list),3)
                for call in write.call_args_list:
                    self.assertNotIn(key,call.args[3])
                    self.assertNotIn('/issues/7',call.args[3])

    def test_wiki_shows_verified_queue_without_linking_legacy_events(self):
        m=self.manager()
        project='org/project'; key=f'{project}#7'; dash='org/console'
        cfg={'name':'fixture','publication':{'signature':'hermes/fixture'},'dashboard':{'repo':dash,'repo_id':10},
             'repositories':{project:{'repo_id':21,'kind':'owned'}}}
        metadata={repo:{'id':id_,'has_wiki':True,'private':False,'visibility':'public'} for repo,id_ in ((project,21),(dash,10))}
        with tempfile.TemporaryDirectory() as tmp,m.State(Path(tmp),'fixture') as state:
            items={key:{'project':project,'state':'queued'}}
            state.data['events']=[{'kind':'started','at':'2026-01-01T00:00:00Z','item':key}]
            with (patch.object(m,'identity'),patch.object(m,'verified_issue',return_value=({'id':29},[])),
                  patch.object(m,'publish_wiki',side_effect=lambda *a,**k:{'status':'published'}) as write):
                m.wiki_pages(cfg,state,items,[],[],[],metadata,lambda:None)
                pages={c.args[2]:c.args[3] for c in write.call_args_list}
                self.assertIn(key,pages['Manager-fixture.md'])
                self.assertNotIn(key,pages['Manager-fixture-Log.md'])

    def test_wiki_rechecks_route_of_queued_issue_before_push(self):
        m=self.manager(); project='org/project'; key=f'{project}#7'; dash='org/console'
        cfg={'name':'fixture','publication':{'signature':'hermes/fixture'},'dashboard':{'repo':dash,'repo_id':10},
             'repositories':{project:{'repo_id':21,'kind':'owned'}}}
        metadata={repo:{'id':i,'has_wiki':True,'private':False,'visibility':'public'} for repo,i in ((project,21),(dash,10))}
        with tempfile.TemporaryDirectory() as tmp,m.State(Path(tmp),'fixture') as state:
            auths={repo:Mock() for repo in metadata}
            for repo in auths:
                auths[repo].canonical_repo.return_value=metadata[repo]
                auths[repo].issue_record.return_value={'id':29,'state':'closed'}
            that_check=[({'id':29},[]),m.Blocked('issue-route-changed'),m.Blocked('issue-route-changed')]
            pushed=[]
            def publish(_git,url,page,content,_scratch,before_push):
                before_push(); pushed.append((url,page))
                return {'status':'published'}
            with (patch.object(m,'identity',side_effect=lambda _,repo:auths[repo]),
                  patch.object(m,'verified_issue',side_effect=that_check),
                  patch.object(m,'publish_wiki',side_effect=publish)):
                m.wiki_pages(cfg,state,{key:{'project':project,'state':'queued'}},[],[],[],metadata,lambda:None)
            self.assertEqual(pushed,[])

    def test_wiki_omits_changed_live_issue_but_keeps_unrouted_dashboard_issue(self):
        m=self.manager()
        project='org/project'; dash='org/console'; stale=f'{project}#7'; unrouted=f'{dash}#8'
        cfg={'name':'fixture','publication':{'signature':'hermes/fixture'},'dashboard':{'repo':dash,'repo_id':10},
             'repositories':{project:{'repo_id':21,'kind':'owned'}}}
        metadata={repo:{'id':id_,'has_wiki':True,'private':False,'visibility':'public'} for repo,id_ in ((project,21),(dash,10))}
        items={stale:{'project':project,'state':'waiting',
                      'issue_binding':{'source_repo_id':21,'target_repo_id':21,'issue_id':29}},
               unrouted:{'project':None,'state':'human'}}
        with tempfile.TemporaryDirectory() as tmp,m.State(Path(tmp),'fixture') as state:
            source=Mock(); source.issue_record.return_value={'id':45,'title':'Needs a target',
                'state':'open','labels':[{'name':'hermes:fixture'}]}
            target=Mock(); target.issue_record.return_value={'id':46}
            pages={}
            def publish(_git,url,page,content,_scratch,before_push):
                before_push(); pages[url,page]=content
                return {'status':'published'}
            source.canonical_repo.return_value=metadata[dash]
            target.canonical_repo.return_value=metadata[project]
            with (patch.object(m,'identity',side_effect=lambda cfg,repo:{dash:source,project:target}[repo]),
                  patch.object(m,'publish_wiki',side_effect=publish)):
                m.wiki_pages(cfg,state,items,[],[],[],metadata,lambda:None)
            self.assertIn(unrouted,pages[m.wiki_url(dash),'Manager-fixture.md'])
            self.assertNotIn(unrouted,pages[m.wiki_url(project),'Manager-fixture.md'])
            self.assertFalse(any(stale in page for page in pages.values()))

    def test_route_can_be_repaired_before_first_attempt(self):
        m = self.manager()
        items = {}
        m.refresh_routes(items, {'org/dash#2': None})
        self.assertEqual(items['org/dash#2']['state'], 'human')
        m.refresh_routes(items, {'org/dash#2': 'org/project'})
        self.assertEqual(items['org/dash#2'], {'project': 'org/project', 'state': 'queued'})

    def test_bundle_handoff_uses_objects_not_worker_git_configuration(self):
        self.manager()
        gh = importlib.import_module('github_io')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / 'worker'
            clean = root / 'clean.git'
            gh.call(['git','init','-q',str(worker)])
            gh.call(['git','-C',str(worker),'-c','user.name=fixture','-c','user.email=f@example.invalid',
                     'commit','--quiet','--allow-empty','-m','candidate'])
            expected = gh.call(['git','-C',str(worker),'rev-parse','HEAD']).strip()
            handoff = worker / 'candidate.bundle'
            gh.call(['git','-C',str(worker),'bundle','create',str(handoff),'HEAD'])
            gh.call(['git','-C',str(worker),'config','--local','core.hooksPath','not-approved'])
            gh.call(['git','init','-q','--bare','--template=',str(clean)])
            gh.import_bundle(clean,handoff,expected)
            self.assertEqual(gh.call(['git','-C',str(clean),'cat-file','-t',expected]).strip(),'commit')
            with self.assertRaisesRegex(gh.Blocked, 'reviewed-head'):
                gh.import_bundle(clean,handoff,'b' * 40)

    def test_dismissed_review_cannot_restore_human_ready(self):
        m = self.manager()
        sha = 'a' * 40
        with tempfile.TemporaryDirectory() as tmp, patch.object(m,'identity') as identity, patch.object(m,'verify_saved_issue'):
            with m.State(Path(tmp),'alpha') as state:
                row=state.begin('org/project#1','org/project')
                row.update(state='human',pr=6,review_id=11,reviewed_head=sha,published_sha=sha)
                auth=identity.return_value
                auth.pull_state.return_value={'head':sha,'checks':'pass','state':'working','draft':False}
                auth.api.side_effect=[{'head':{'sha':sha},'user':{'login':'coder'}},
                                      {'id':11,'state':'DISMISSED','commit_id':sha}]
                m.reconcile({},state,Mock())
                self.assertEqual(row['state'],'working')

    def test_reconcile_holds_externally_advanced_pull_request_head(self):
        m = self.manager()
        published, advanced = 'a' * 40, 'b' * 40
        with tempfile.TemporaryDirectory() as tmp, m.State(Path(tmp),'alpha') as state:
            row = state.begin('org/project#1','org/project')
            row.update(state='human',pr=6,published_sha=published,reviewed_head=advanced,review_id=11)
            with patch.object(m,'identity') as auth, patch.object(m,'verify_review') as review, patch.object(m,'verify_saved_issue'):
                auth.return_value.pull_state.return_value = {'head':advanced,'checks':'pass','state':'working','draft':False}
                m.reconcile({},state,Mock())
                review.assert_not_called()
                self.assertEqual(row['state'],'working')
                self.assertEqual(row['hold'],'unverified-pr-head')
                self.assertEqual(row['published_sha'],published)
                self.assertNotIn('checks',row)
                self.assertNotIn('reviewed_head',row)

    def test_record_review_rejects_any_head_other_than_published_head(self):
        m = self.manager()
        key, project = 'org/project#1', 'org/project'
        published, advanced = 'a' * 40, 'b' * 40
        cfg = {'repositories':{project:{'base':'main'}}}
        with tempfile.TemporaryDirectory() as tmp, m.State(Path(tmp),'alpha') as state:
            row = state.begin(key,project)
            row.update(state='working',pr=6,published_sha=published,pr_base='main',pr_branch='hermes/alpha/1234567890123456')
            with patch.object(m,'identity') as auth, patch.object(m,'verify_review') as review:
                auth.return_value.pull_state.return_value = {'head':advanced,'checks':'pass','state':'working','draft':False}
                with self.assertRaisesRegex(m.Blocked,'unverified-pr-head'):
                    m.owner_review(cfg,state,key,advanced,11)
                review.assert_not_called()
                self.assertNotIn('reviewed_head',row)

    def test_config_edit_disarms_running_supervisor(self):
        m = self.manager()
        with tempfile.TemporaryDirectory() as tmp, patch.object(m,'load_config',return_value={'name':'new'}):
            root=Path(tmp)
            cfg={'name':'original'}
            m.save_private(root/'activation.json',{'config':m.config_digest(cfg)})
            self.assertFalse(m.live_activation(cfg,root,root/'config.yaml'))

    @contextmanager
    def direct_command_profile(self, *, running=True):
        m = self.manager()
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp).resolve()
            root = m.private(profile/'github-project-manager')
            config = root/'config.yaml'
            example = m.ROOT/'examples'/'project.yaml'
            config.touch(mode=0o600)
            config.write_text(example.read_text().replace('workspace: null', 'workspace: fixture'))
            cfg = m.load_config(config)
            digest = m.config_digest(cfg)
            head = 'a'*40
            mark = {'nonce':'b'*32,'scope':['default','fixture'],'pane':'fixture:manager',
                    'state':'running','proof':None,'apply':True}
            hb = {'nonce':mark['nonce'],'at':m.stamp(),'mode':'apply','config':digest,
                  'config_path':str(config),'head':head}
            m.save_private(root/'activation.json',{'config':digest,'at':m.stamp()})
            m.save_private(root/'manager-launch.json',mark)
            m.save_private(root/'heartbeat.json',hb)
            with m.lock(root,'pass.lock'), m.lock(root,m.CONTROLLER_LOCK):
                pass
            lease = m.lock(root,m.CONTROLLER_LOCK) if running else nullcontext()
            with (patch.object(m,'home',return_value=profile),
                  patch.object(m,'ROOT',profile/'skills'/m.SELF),
                  patch.object(m,'loaded_skill_head',return_value=head),
                  m.lock(root,'manager.live.lock'), lease):
                yield root,cfg,mark,hb

    def run_direct_command(self, *args):
        output = io.StringIO()
        stdin = io.TextIOWrapper(io.BytesIO(b'Continue'))
        with (patch.object(sys,'argv',['manager.py',*args]), patch.object(sys,'stdin',stdin),
              redirect_stdout(output)):
            code = self.manager().main()
        return code,json.loads(output.getvalue())

    def test_direct_commands_require_activation_and_verified_apply_controller(self):
        m = self.manager()
        commands = {
            'follow_up':('follow-up','org/repo#1'),
            'publish_item':('publish','org/repo#1','--reviewed-head','a'*40),
            'gap':('capability-gap',m.SELF,'fixture-gap'),
        }
        cases = ('armed preview','preview launch with apply heartbeat','disarmed','restart hold','update journal','stale heartbeat',
                 'stopped controller','wrong launch scope','wrong launch nonce','unverified launch',
                 'wrong revision','legacy heartbeat','different config path','busy state','permitted apply')
        for name,args in commands.items():
            for case in cases:
                with (self.subTest(command=args[0],case=case),
                      self.direct_command_profile(running=case!='stopped controller') as (root,_,mark,hb),
                      patch.object(m,name,return_value={'reached':True}) as handler):
                    argv = args
                    if case=='armed preview':
                        mark['apply'],hb['mode'] = False,'preview'
                    elif case=='preview launch with apply heartbeat':
                        mark['apply'] = False
                    elif case=='disarmed':
                        config = root/'config.yaml'
                        config.write_text(config.read_text().replace('network: false','network: true'))
                    elif case=='restart hold':
                        hb['mode'] = 'restart-required'
                    elif case=='update journal':
                        (root/m.UPDATE_INTENT).symlink_to(root/'missing-intent')
                    elif case=='stale heartbeat':
                        hb['at'] = m.stamp(0)
                    elif case=='wrong launch scope':
                        mark['scope'] = ['default','other']
                    elif case=='wrong launch nonce':
                        mark['nonce'] = 'c'*32
                    elif case=='unverified launch':
                        mark['state'] = 'launched'
                    elif case=='wrong revision':
                        hb['head'] = 'c'*40
                    elif case=='legacy heartbeat':
                        del hb['head']
                    elif case=='different config path':
                        config = root/'alternate.yaml'
                        config.write_bytes((root/'config.yaml').read_bytes())
                        argv = ('--config',str(config),*args)
                    m.save_private(root/'manager-launch.json',mark)
                    m.save_private(root/'heartbeat.json',hb)
                    state_lock = m.lock(root,'pass.lock') if case=='busy state' else nullcontext()
                    with state_lock:
                        code,result = self.run_direct_command(*argv)
                    if case=='permitted apply':
                        self.assertEqual((code,result),(0,{'ok':True,'result':{'reached':True}}))
                        handler.assert_called_once()
                    else:
                        self.assertEqual((code,result['ok']),(1,False))
                        handler.assert_not_called()
                        self.assertFalse((root/'state.json').exists())
                        if case=='update journal':
                            self.assertTrue((root/m.UPDATE_INTENT).is_symlink())

    def test_quota_setup_remains_available_without_activation_or_controller(self):
        m = self.manager()
        with self.direct_command_profile(running=False) as (root,_,_,_), patch.object(m,'Adapter') as adapter:
            (root/'activation.json').unlink()
            adapter.return_value.scope = ('default','fixture')
            adapter.return_value.provision.return_value = 'fixture:quota'
            adapter.return_value.quota.return_value = {'usable_windows':['primary']}
            code,result = self.run_direct_command('quota-setup')
            self.assertEqual((code,result['result']), (0,{'pane_id':'fixture:quota','quota_read':True}))
            adapter.return_value.provision.assert_called_once()

    def test_follow_up_rechecks_after_agent_cli_help(self):
        m = self.manager()
        for case in ('disarmed','controller replaced','permitted'):
            with self.subTest(case=case), self.direct_command_profile() as (root,cfg,mark,hb):
                key = 'org/repo#1'
                row = {'project':'org/repo','state':'waiting','scope':['default','fixture'],
                       'pane':'fixture:worker','thread':'thread','turn':'last','worker_network':False}
                with m.State(root,cfg['name']) as state:
                    state.data['items'][key] = row
                    state.save()
                adapter = object.__new__(m.Adapter)
                adapter.scope = ('default','fixture')
                entry=root/'mock-agent-cli'; entry.touch(mode=0o600)
                adapter.paths = {m.AGENT:entry}

                def help_text(*args,**kwargs):
                    if case=='disarmed':
                        (root/'activation.json').unlink()
                    elif case=='controller replaced':
                        nonce = 'f'*32
                        m.save_private(root/'manager-launch.json',dict(mark,nonce=nonce))
                        m.save_private(root/'heartbeat.json',dict(hb,nonce=nonce))
                    return '--thread-id --after-turn-id --override-quota'

                with (patch.object(adapter,'entry',return_value=str(entry)),
                      patch.object(m,'Adapter',return_value=adapter), patch.object(m,'bound_issue'),
                      patch.object(m,'check_health',return_value={'blocks':[]}),
                      patch.object(m,'call',side_effect=help_text),
                      patch.object(adapter,'run',return_value={'thread_id':'thread','turn_id':'next','answer':'done'}) as send):
                    code,result = self.run_direct_command('follow-up',key)
                    stored = m.load_private(root/'state.json')
                    if case=='permitted':
                        self.assertEqual((code,result['result']['turn_id']),(0,'next'))
                        send.assert_called_once_with(m.AGENT,'steer','--pane','fixture:worker','--thread-id','thread',
                                                     '--after-turn-id','last','--prompt-stdin','--seconds','120',
                                                     data='Continue',timeout=205)
                        self.assertEqual(stored['items'][key]['turn'],'next')
                    else:
                        self.assertEqual((code,result['ok']),(1,False))
                        send.assert_not_called()
                        self.assertEqual(stored['events'],[])
                        self.assertEqual(stored['items'][key],row)

    def test_publish_rechecks_at_each_github_write(self):
        m = self.manager()
        for phase in ('bundle','push','push journal','push expiry','no-op','pr create','pr ref','pr journal','pr expiry','foreign pr','permitted'):
            with self.subTest(phase=phase), self.direct_command_profile() as (root,cfg,_,_):
                gh = importlib.import_module('github_io')
                project = cfg['updates'][m.SELF]
                key = f'{project}#1'
                base,expected,checkout = 'c'*40,'d'*40,'e'*16
                (root/'work'/checkout).mkdir(parents=True)
                with m.State(root,cfg['name']) as state:
                    state.data['items'][key] = {'project':project,'state':'waiting','base_sha':base,'checkout':checkout}
                    state.save()
                request=root/'request.json'; signature=root/'request.sig'
                for path in (request,signature):
                    path.touch(mode=0o600); path.write_text('fixture')
                request.write_text(m.approval_message({}))
                auth = gh.Identity(project,{'type':'gh','login':'fixture'},'hermes/fixture')
                writes = []
                pushed_head_reads=[]

                def git(args,**kwargs):
                    if args[0]=='ls-remote':
                        ref = args[-1]
                        if ref=='refs/heads/main':
                            return f'{base}\t{ref}'
                        if 'push' in writes:
                            pushed_head_reads.append(ref)
                            if len(pushed_head_reads)==2: disarm_at('pr ref')
                        return f'{expected}\t{ref}' if 'push' in writes else ''
                    if args[2]=='push':
                        writes.append('push')
                        if phase=='no-op': return f'=\t{args[-1]}\t[up to date]\nDone\n'
                        return f'*\t{args[-1]}\t[new branch]\nDone\n'
                    return ''

                def github(args,**kwargs):
                    self.assertEqual(args[:2],['pr','create'])
                    writes.append('pr create')

                def disarm_at(point):
                    if phase==point:
                        (root/'activation.json').unlink()

                def existing(*a,**k):
                    if phase=='foreign pr' and len(pushed_head_reads)==2:
                        return 91
                    return 7 if 'pr create' in writes else None

                event=m.State.event
                clock=[m.time.time()]
                expire_during_guard=[]
                def journal(state,kind,item=None):
                    event(state,kind,item)
                    if kind in ('publication-started','pr-creation-started'):
                        disarm_at('push journal' if kind=='publication-started' else 'pr journal')
                        if phase==('push expiry' if kind=='publication-started' else 'pr expiry'):
                            expire_during_guard.append(True)
                real_guard=m.direct_command_guard
                def guard_factory(*a,**k):
                    validate=real_guard(*a,**k)
                    def guard():
                        validate()
                        if expire_during_guard:
                            expire_during_guard.pop()
                            clock[0]+=m.APPROVAL_LIFETIME
                    return guard
                fake_time=Mock(wraps=m.time)
                fake_time.time.side_effect=lambda:clock[0]

                with (patch.object(m,'identity',return_value=auth), patch.object(m,'publication_scope',return_value={'base_sha':base}),
                      patch.object(m,'verify_publication_approval'),
                      patch.object(m,'call',return_value=base),
                      patch.object(m,'import_bundle',side_effect=lambda *a: disarm_at('bundle')),
                      patch.object(auth,'check_pr_target') as target, patch.object(auth,'verify') as verify,
                      patch.object(auth,'existing_pr',side_effect=existing),patch.object(m.State,'event',journal),
                      patch.object(m,'time',fake_time),patch.object(m,'direct_command_guard',side_effect=guard_factory),
                      patch.object(auth,'git',side_effect=git), patch.object(auth,'gh',side_effect=github)):
                    target.side_effect = lambda *a: disarm_at('push') if target.call_count==2 else None
                    verify.side_effect = lambda: disarm_at('pr create') if verify.call_count==2 else None
                    code,result = self.run_direct_command('publish',key,'--reviewed-head',expected,
                                                          '--approval-request',str(request),'--approval-signature',str(signature))
                self.assertEqual((code,result['ok']), (0,True) if phase=='permitted' else (1,False))
                expected_writes = {'bundle':[],'push':[],'push journal':[],'push expiry':[],'no-op':['push'],'pr create':['push'],
                                   'pr ref':['push'],'pr journal':['push'],'pr expiry':['push'],
                                   'foreign pr':['push'],'permitted':['push','pr create']}
                self.assertEqual(writes,expected_writes[phase])
                if phase in ('no-op','pr ref'):
                    row=m.load_private(root/'state.json')['items'][key]
                    self.assertEqual(row['state'],'uncertain')
                    self.assertIs(row.get('push_confirmed') is True,phase=='pr ref')
                if phase in ('push journal','push expiry','pr journal','pr expiry','foreign pr'):
                    row=m.load_private(root/'state.json')['items'][key]
                    self.assertNotIn('pr_create_attempted',row)
                    if phase in ('push journal','push expiry'):
                        self.assertEqual(row['state'],'waiting')
                        self.assertNotIn('publish_sha',row)

    def test_capability_gap_rechecks_after_preflight_for_label_and_issue(self):
        m = self.manager()
        for phase in ('label','issue','permitted'):
            with self.subTest(phase=phase), self.direct_command_profile() as (root,cfg,_,_):
                gh = importlib.import_module('github_io')
                project = cfg['updates'][m.SELF]
                label = f'hermes:{cfg["name"]}'
                auth = gh.Identity(project,{'type':'gh','login':'fixture'},'hermes/fixture')
                writes = []

                def github(args,**kwargs):
                    if args[:2]==['api','--paginate']:
                        return label if phase=='issue' else ''
                    self.assertEqual(args[:3],['api','--method','POST'])
                    writes.append(args[3].rsplit('/',1)[1])

                def issues():
                    return [{'number':7,'title':'Improve project management capability: fixture-gap','labels':[label]}] if 'issues' in writes else []

                with (patch.object(m,'identity',return_value=auth), patch.object(auth,'verify'),
                      patch.object(auth,'api',return_value={'name':label}), patch.object(auth,'canonical_repo') as canonical,
                      patch.object(auth,'issues',side_effect=issues), patch.object(auth,'gh',side_effect=github)):
                    def canonical_repo():
                        if phase!='permitted' and canonical.call_count==2:
                            m.save_private(root/m.UPDATE_INTENT,{'name':m.SELF})
                    canonical.side_effect = canonical_repo
                    code,result = self.run_direct_command('capability-gap',m.SELF,'fixture-gap')
                self.assertEqual((code,result['ok']), (0,True) if phase=='permitted' else (1,False))
                self.assertEqual(writes,['labels','issues'] if phase=='permitted' else [])

    def test_stale_privacy_snapshot_blocks_wiki_push(self):
        m = self.manager()
        with patch.object(m,'identity') as auth:
            auth.return_value.canonical_repo.return_value={'id':11,'private':False,'visibility':'public','has_wiki':True}
            with self.assertRaisesRegex(m.Blocked,'wiki-visibility-changed'):
                m.check_wiki_visibility({}, {'org/console':{'id':11,'private':True,'visibility':'private'}}, 'org/console', [])

    def test_cli_does_not_accept_apply_from_an_arbitrary_terminal(self):
        self.manager()
        script=Path(__file__).resolve().parents[1]/'scripts'/'manager.py'
        r=subprocess.run([sys.executable,'-B',str(script),'tick','--apply'],capture_output=True,text=True)
        self.assertEqual(r.returncode,2)
        self.assertIn('unrecognized arguments: --apply',r.stderr)

    def test_title_edit_cannot_change_approved_dashboard_target(self):
        m=self.manager()
        cfg={'name':'alpha','repositories':{'org/a':{},'org/b':{}}}
        label=m.project_label('org/a')
        self.assertEqual(m.dashboard_target(cfg,'[org/a] Address this',['hermes:alpha',label]),'org/a')
        self.assertIsNone(m.dashboard_target(cfg,'[org/b] Address this',['hermes:alpha',label]))


    def test_quota_legacy_settings_are_optional(self):
        m=self.manager()
        example=(Path(__file__).resolve().parents[1]/'examples'/'project.yaml').read_text(encoding='utf-8')
        values=m.yaml.safe_load(example)
        values.pop('quota',None)
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config.yaml'
            config.touch(mode=0o600)
            config.write_text(m.yaml.safe_dump(values),encoding='utf-8')
            self.assertNotIn('quota',m.load_config(config))
            legacy={'short_floor':12,'long_floor':7,'per_worker_reserve':3,'weekly_pace_percent':90,'allow_weekly_only':False}
            config.write_text(m.yaml.safe_dump(dict(values,quota=legacy)),encoding='utf-8')
            self.assertEqual(m.load_config(config)['quota'],legacy)

    def test_oversize_state_cannot_replace_last_readable_record(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp, patch.object(m,'MAX_STATE_BYTES',80,create=True):
            path=Path(tmp)/'state.json'
            m.save_private(path,{'state':'original'})
            with self.assertRaisesRegex(m.Blocked,'state-size-limit'):
                m.save_private(path,{'state':'a'*80})
            self.assertEqual(m.load_private(path),{'state':'original'})

    def test_incidental_check_cannot_substitute_for_configured_required_check(self):
        self.manager()
        gh = importlib.import_module('github_io')
        auth = gh.Identity('org/repo', {'type':'gh','login':'owner'}, 'hermes/test')
        head = 'a'*40
        check=dict(name='lint',head_sha=head,status='completed',conclusion='success')
        with patch.object(auth,'api',side_effect=[dict(number=7,head={'sha':head},state='open',draft=False),
                                                   dict(sha=head,total_count=0)]), \
             patch.object(auth,'gh',return_value=json.dumps(check)):
            self.assertEqual(auth.pull_state(7,required=['tests'])['checks'],'missing')

    def test_retirement_requires_absence_and_does_not_replay(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp, m.State(Path(tmp),'alpha') as state, patch.object(m,'Adapter') as adapter:
            key='org/repo#1'
            row=state.begin(key,'org/repo')
            row.update(state='uncertain',scope=['instance','workspace'],pane='workspace:p1',pr=12)
            adapter.return_value.scope=('instance','workspace')
            adapter.return_value.layout.return_value={'tabs':[{'panes':[{'pane_id':row['pane']}]}]}
            with self.assertRaisesRegex(m.Blocked,'worker-pane-still-present'):
                m.retire_uncertain({},state,key)
            adapter.return_value.layout.return_value={'tabs':[{'panes':[]}]}
            self.assertFalse(m.retire_uncertain({},state,key)['work_replayed'])
            self.assertEqual(row['state'],'human')
            self.assertEqual(row['pr'],12)
            adapter.return_value.start.assert_not_called()

    def test_incomplete_or_duplicate_inventory_is_not_evidence_of_absence(self):
        m=self.manager()
        for view in ({}, {'tabs':[{}]}, {'tabs':[{'panes':[{'pane_id':'p'},{'pane_id':'p'}]}]}):
            with self.assertRaisesRegex(m.Blocked,'pane-layout-unverified'):
                m.pane_inventory(view)

    def test_update_pass_polls_all_three_and_throttles_rechecks(self):
        m=self.manager()
        names=(m.PANE,m.AGENT,m.SELF)
        heads={n:{'name':n,'local_head':'a'*40,'remote_head':'b'*40,'status':'update-available'} for n in names}
        cfg={'schedule':{'skill_update_interval_seconds':1800}}
        guard=Mock()
        state=Mock(data={'items':{}})
        with patch.object(m,'version_report',return_value=list(heads.values())) as probe, \
             patch.object(m,'sync_candidate',create=True,side_effect=lambda c,s,n,h,g: dict(heads[n],status='updated')) as update:
            upkeep=m.SkillUpkeep(can_install_self=True)
            upkeep.poll(cfg,state,True,guard,100)
            upkeep.poll(cfg,state,True,guard,101)
            self.assertEqual(probe.call_count,1)
            self.assertEqual([c.args[2] for c in update.call_args_list],list(names))
            upkeep.poll(cfg,state,True,guard,1900)
            self.assertEqual(probe.call_count,2)

    def test_update_pass_never_applies_in_preview(self):
        m=self.manager()
        row={'name':m.AGENT,'local_head':'a'*40,'remote_head':'b'*40,'status':'update-available'}
        with patch.object(m,'version_report',return_value=[row]),patch.object(m,'sync_candidate',create=True) as update:
            self.assertEqual(m.SkillUpkeep().poll({'schedule':{}},Mock(),False,Mock(),100),[row])
            update.assert_not_called()

    def test_clean_main_rejects_hidden_tracked_modification(self):
        m=self.manager()
        for flag in ('--assume-unchanged','--skip-worktree'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as tmp:
                folder=Path(tmp)
                def git(*args): return m.call(['git','-C',str(folder),*args]).strip()
                git('init','-qb','main'); (folder/'SKILL.md').write_text('approved')
                git('add','SKILL.md'); git('-c','user.name=fixture','-c','user.email=f@example.invalid','commit','-qm','approved')
                self.assertTrue(m.clean_main(folder))
                git('update-index',flag,'SKILL.md'); (folder/'SKILL.md').write_text('hidden edit')
                self.assertEqual(git('status','--porcelain'),'')
                self.assertFalse(m.clean_main(folder))

    def test_update_real_fast_forward_and_native_lock_hold(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            skills=m.private(root/'skills')
            remote=root/'remote'
            path=skills/m.AGENT
            repo='org/agent-manager'
            url=f'https://github.com/{repo}.git'
            def git(cwd,*args):
                return m.call(['git','-C',str(cwd),'-c','user.name=fixture','-c','user.email=fixture@example.invalid',*args]).strip()
            remote.mkdir()
            git(remote,'init','-q','--initial-branch=main')
            (remote/'SKILL.md').write_text('version one',encoding='utf-8')
            git(remote,'add','SKILL.md'); git(remote,'commit','-qm','version one')
            old=git(remote,'rev-parse','HEAD')
            git(root,'clone','-q',str(remote),str(path)); git(path,'remote','set-url','origin',url)
            (remote/'SKILL.md').write_text('version two',encoding='utf-8')
            git(remote,'commit','-qam','version two')
            expected=git(remote,'rev-parse','HEAD')
            auth=Mock(repo=repo,repo_id=37,reviewer_ids=[51]); auth.spec={'login':'publisher[bot]'}
            auth.git.side_effect=lambda args,**kw: m.call(['git',*[str(remote) if x==url else x for x in args]],**kw)
            auth.check_state.return_value='missing'
            state=Mock(root=root,data={'items':{}})
            cfg={'updates':{m.AGENT:repo},'dashboard':{'repo':'org/dash'},'repositories':{repo:{}}}
            ledger=skills/'.usage.json'
            m.save_private(ledger,{m.AGENT:{'locked':True}})
            with patch.object(m,'home',return_value=root),patch.object(m,'identity',return_value=auth):
                with self.assertRaisesRegex(m.Blocked,'skill-locked-operator-update-required'):
                    m.sync_candidate(cfg,state,m.AGENT,expected,Mock())
                auth.git.assert_not_called()
                self.assertEqual(git(path,'rev-parse','HEAD'),old)
                self.assertEqual(m.load_private(ledger),{m.AGENT:{'locked':True}})
                m.save_private(ledger,{})
                ledger.chmod(0o644)
                (path/'draft').write_text('uncommitted',encoding='utf-8')
                with self.assertRaisesRegex(m.Blocked,'do-not-overwrite-local-skill-changes'):
                    m.sync_candidate(cfg,state,m.AGENT,expected,Mock())
                (path/'draft').unlink()

                with self.assertRaisesRegex(m.Blocked,'update-requires-exact-head-green-ci'):
                    m.sync_candidate(cfg,state,m.AGENT,expected,Mock())
                self.assertEqual(git(path,'rev-parse','HEAD'),old)
                auth.check_state.return_value='pass'
                auth.gh.return_value=''
                with self.assertRaisesRegex(m.Blocked,'update-requires-reviewed-pr-provenance'):
                    m.sync_candidate(cfg,state,m.AGENT,expected,Mock())
                self.assertEqual(git(path,'rev-parse','HEAD'),old)
                pr={'number':7,'merge_commit_sha':expected,'merged_at':'2026-01-01T01:00:00Z',
                    'base':{'ref':'main','sha':old,'repo':{'id':37}},'head':{'sha':expected},
                    'user':{'login':'publisher[bot]','id':52}}
                review={'id':11,'state':'APPROVED','user_id':51,'commit_id':expected,'submitted_at':'2026-01-01T00:00:00Z',
                        'user':{'login':'reviewer','id':51},'author_association':'COLLABORATOR',
                        'pull_request_url':f'https://api.github.com/repos/{repo}/pulls/7'}
                auth.gh.side_effect=lambda args: json.dumps(pr if '/commits/' in args[2] else review)
                auth.api.side_effect=lambda endpoint: review if '/reviews/' in endpoint else pr
                review['body']=f'Accepted this range.\n\nhermes-update-base: {old}\n'
                with self.assertRaisesRegex(m.Blocked,'update-reviewed-range-mismatch'):
                    m.sync_candidate(cfg,state,m.AGENT,expected,Mock())
                self.assertEqual(git(path,'rev-parse','HEAD'),old)
                git(remote,'commit','--amend','-qm',f'version two\n\nHermes-Update-Base: {old}')
                expected=git(remote,'rev-parse','HEAD')
                pr['merge_commit_sha']=pr['head']['sha']=review['commit_id']=expected
                review['body']='The approved commit contains the range.'
                pr['base']['sha']='b'*40
                with self.assertRaisesRegex(m.Blocked,'update-requires-reviewed-pr-provenance'):
                    m.sync_candidate(cfg,state,m.AGENT,expected,Mock())
                pr['base']['sha']=old
                calls=[]
                def switch_branch():
                    calls.append(None)
                    if len(calls)==2: git(path,'switch','-qc','other')
                with self.assertRaisesRegex(m.Blocked,'skill-changed-during-update'):
                    m.sync_candidate(cfg,state,m.AGENT,expected,switch_branch)
                self.assertEqual(git(path,'rev-parse','refs/heads/other'),old)
                git(path,'switch','-q','main')
                real_save=m.save_private
                def save_then_switch(p,value):
                    real_save(p,value)
                    if p.name==m.UPDATE_INTENT: git(path,'switch','-q','other')
                with patch.object(m,'save_private',side_effect=save_then_switch):
                    with self.assertRaisesRegex(m.Blocked,'skill-changed-during-update'):
                        m.sync_candidate(cfg,state,m.AGENT,expected,Mock())
                self.assertEqual(git(path,'rev-parse','HEAD'),old)
                git(path,'switch','-q','main')
                row=m.sync_candidate(cfg,state,m.AGENT,expected,Mock())
                journal=root/m.UPDATE_INTENT
                self.assertFalse(journal.exists())
                m.save_private(journal,{'name':m.AGENT,'repo':repo,'before':old,'target':expected})
                draft=path/'partial-update'
                draft.write_text('keep pending',encoding='utf-8')
                with self.assertRaisesRegex(m.Blocked,'skill-update-intent-unverified'):
                    m.check_update_intent(cfg,root,running=True)
                self.assertTrue(journal.exists())
                draft.unlink()
                m.save_private(ledger,{m.AGENT:{'locked':True}})
                m.check_update_intent(cfg,root,running=True)
                self.assertFalse(journal.exists())
            self.assertEqual(row['status'],'updated')
            self.assertEqual(git(path,'rev-parse','HEAD'),expected)
            self.assertEqual((path/'SKILL.md').read_text(encoding='utf-8'),'version two')
            skills.chmod(0o755)  # The profile root is still private.
            (skills/'.usage.json.lock').chmod(0o644)
            with m.curator_update_guard(skills,m.AGENT,inspect_only=True):
                self.assertEqual(git(path,'rev-parse','HEAD'),expected)

    def test_manual_self_sync_requires_current_dependencies(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'version_report',return_value=[
                {'name':m.PANE,'status':'current'},{'name':m.AGENT,'status':'update-available'}]), \
             patch.object(m,'sync_candidate') as update:
            state=Mock(root=Path(tmp).resolve())
            with self.assertRaisesRegex(m.Blocked,'dependencies-not-current'):
                m.sync_skill({},state,m.SELF,'a'*40)
            update.assert_not_called()

    def test_unreadable_curator_record_does_not_grant_update(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp:
            skills=m.private(Path(tmp)/'skills')
            for record in (None,{m.PANE:{'locked':'unknown'}}):
                if record is not None: m.save_private(skills/'.usage.json',record)
                with self.assertRaisesRegex(m.Blocked,'lock-status-not-verified'),m.curator_update_guard(skills,m.PANE):
                    self.fail('invalid lock data granted access')

    def test_self_update_waits_for_dependencies_and_stops_after_update(self):
        m=self.manager()
        cfg={'schedule':{}}
        state=Mock(data={'items':{}})
        candidate={'name':m.SELF,'local_head':'a'*40,'remote_head':'b'*40,'status':'update-available'}
        with patch.object(m,'version_report',side_effect=[
                [dict(candidate),{'name':m.PANE,'status':'current'},{'name':m.AGENT,'status':'unavailable'}],
                [dict(candidate),{'name':m.PANE,'status':'current'},{'name':m.AGENT,'status':'current'}]]) as probe, \
             patch.object(m,'sync_candidate',return_value={'status':'updated','restart_required':True}) as update:
            upkeep=m.SkillUpkeep(can_install_self=True)
            self.assertEqual(upkeep.poll(cfg,state,True,Mock(),100)[0]['hold'],'dependencies-not-current')
            update.assert_not_called()
            self.assertEqual(upkeep.poll(cfg,state,True,Mock(),1900)[0]['status'],'updated')
            self.assertTrue(upkeep.restart_required)
            upkeep.poll(cfg,state,True,Mock(),3800)
            self.assertEqual(probe.call_count,2)

    def test_update_stays_held_for_unfinished_worker(self):
        m=self.manager()
        cfg={'updates':{m.AGENT:'org/skill'}}
        state=Mock(data={'items':{'org/project#1':{'state':'waiting'}}})
        with patch.object(m,'identity') as auth:
            with self.assertRaisesRegex(m.Blocked,'drain-work-before-skill-update'):
                m.sync_candidate(cfg,state,m.AGENT,'a'*40,Mock())
            auth.assert_not_called()

    def test_controller_holds_dispatch_after_its_own_scheduled_update(self):
        m=self.manager()
        with tempfile.TemporaryDirectory() as tmp, patch.object(m,'SkillUpkeep') as upkeep, \
             patch.object(m,'tick') as tick, patch.object(m,'live_activation',return_value=True), \
             patch.object(m.subprocess,'Popen') as launch, patch.object(m.time,'sleep'):
            root=Path(tmp).resolve()
            child=launch.return_value.__enter__.return_value
            child.poll.side_effect=[None,0]
            child.returncode=0
            updater=upkeep.return_value
            updater.restart_required=False
            def updated(*args):
                updater.restart_required=True
                return [{'name':m.SELF,'status':'updated'}]
            updater.poll.side_effect=updated
            result=m.run_controller('fake',root/'seed',{'name':'alpha','schedule':{'interval_seconds':300}},
                                    root,'nonce',True,{},Mock(),root/'config.yaml')
            self.assertEqual(result,0)
            tick.assert_not_called()
            self.assertEqual(m.load_private(root/'last-pass.json')['result']['dispatch_holds'],['manager-restart-required'])
            self.assertEqual(m.load_private(root/'heartbeat.json')['mode'],'restart-required')

    def test_controller_liveness_proof_does_not_gate_scheduled_passes(self):
        m = self.manager()
        for lock_busy in (False,True):
            with (self.subTest(lock_busy=lock_busy),
                  self.direct_command_profile(running=lock_busy) as (root,cfg,mark,hb),
                  patch.object(m,'SkillUpkeep') as upkeep, patch.object(m,'tick',return_value={}) as tick,
                  patch.object(m.subprocess,'Popen') as launch, patch.object(m.time,'sleep')):
                child = launch.return_value.__enter__.return_value
                child.poll.side_effect = [None,0]
                child.returncode = 0
                upkeep.return_value.restart_required = False
                upkeep.return_value.poll.return_value = []

                def check_proof(*args,**kwargs):
                    heartbeat = m.load_private(root/'heartbeat.json')
                    self.assertEqual(heartbeat['mode'],'apply')
                    self.assertEqual(heartbeat['head'],None if lock_busy else hb['head'])
                    self.assertEqual(heartbeat['config_path'],hb['config_path'])
                    with self.assertRaisesRegex(m.Blocked,'manager-busy'), m.lock(root,m.CONTROLLER_LOCK):
                        self.fail('controller lock should be held')
                    return {}

                tick.side_effect = check_proof
                code = m.run_controller('fake',root/'seed',cfg,root,mark['nonce'],True,{},Mock(),root/'config.yaml')
                self.assertEqual(code,0)
                tick.assert_called_once()

    def test_self_intent_requires_controller_start_at_verified_head(self):
        m=self.manager()
        before,target='a'*40,'b'*40
        repo='org/project-manager'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            p=m.private(root/'skills')/m.SELF
            cfg={'updates':{m.SELF:repo}}
            journal=root/m.UPDATE_INTENT
            m.save_private(p.parent/'.usage.json',{})
            m.save_private(journal,{'name':m.SELF,'repo':repo,'before':before,'target':target})
            with patch.object(m,'home',return_value=root),patch.object(m,'ROOT',p), \
                 patch.object(m,'same_installation',return_value=True),patch.object(m,'clean_main',return_value=True), \
                 patch.object(m,'update_git',return_value=target):
                m.save_private(p.parent/'.usage.json',{m.SELF:{'locked':True}})
                with self.assertRaisesRegex(m.Blocked,'manager-restart-required'):
                    m.check_update_intent(cfg,root)
                self.assertTrue(journal.exists())
                with self.assertRaisesRegex(m.Blocked,'manager-restart-required'):
                    m.check_update_intent(cfg,root,running=True,running_head=before)
                self.assertTrue(journal.exists())
                m.check_update_intent(cfg,root,running=True,running_head=target)
                self.assertFalse(journal.exists())

    def test_loaded_code_check_rejects_source_changed_during_startup(self):
        m=self.manager()
        current=b'answer=42\n'
        code=compile(current,'current.py','exec')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'current.py'
            path.write_bytes(b'answer=43\n')
            with self.assertRaisesRegex(m.Blocked,'manager-running-code-changed'):
                m.loaded_blob(path,code)
            path.write_bytes(current)
            self.assertRegex(m.loaded_blob(path,code),r'^[0-9a-f]{40}$')
