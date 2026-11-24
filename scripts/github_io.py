"""GitHub transport. Never take commands, credentials, remotes, or paths from issues."""
from functools import lru_cache
import errno
import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
WORKFLOW = re.compile(r'\.github/workflows/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,100}\.ya?ml\Z')
LOADED_CODE = sys._getframe().f_code


class Blocked(RuntimeError):
    """A precondition failed; do not continue a write."""


@lru_cache(maxsize=1)
def private_primary_group():
    """Accept the owner's conventional private group only on local NSS."""
    try:
        rules={key.strip():value.split() for line in Path('/etc/nsswitch.conf').read_text().splitlines()
               if ':' in (clean:=line.split('#',1)[0]) for key,value in [clean.split(':',1)]}
        if any(rules.get(key,['files']) not in (['files'],['files','systemd']) for key in ('passwd','group','initgroups')):
            return None
        user=pwd.getpwuid(os.getuid()); gid=os.getgid()
        group=grp.getgrgid(gid)
        if (gid!=0 and user.pw_gid==gid and group.gr_name==user.pw_name and
                not set(group.gr_mem)-{user.pw_name} and
                {r.pw_uid for r in pwd.getpwall() if r.pw_gid==gid}=={os.getuid()}):
            return gid
    except (OSError,KeyError,UnicodeError):
        pass
    return None


def has_no_access_acl(path):
    try:
        os.getxattr(path,'system.posix_acl_access')
    except OSError as e:
        return e.errno in (errno.ENODATA,errno.ENOTSUP)
    return False


def trusted_path(path, *, directory=False):
    p=Path(path)
    if not p.is_absolute() or '..' in p.parts:
        return False
    try:
        for candidate in {p,p.resolve(strict=True)}:
            private=False
            for item in (*reversed(candidate.parents),candidate):
                info=item.stat()
                is_dir=item!=candidate or directory
                shared_write=bool(info.st_mode&0o002 or (info.st_mode&0o020 and
                                  (info.st_gid!=private_primary_group() or not has_no_access_acl(item))))
                if (not (stat.S_ISDIR(info.st_mode) if is_dir else stat.S_ISREG(info.st_mode)) or
                        info.st_uid not in (0,os.getuid()) or
                        (not private and shared_write and not (is_dir and info.st_mode&stat.S_ISVTX))):
                    return False
                private=private or (is_dir and info.st_uid==os.getuid() and not info.st_mode&0o011)
    except (OSError,RuntimeError):
        return False
    return True


def trusted_executable(command):
    path=shutil.which(command)
    if path is None or not trusted_path(path):
        raise Blocked('unverified-executable')
    return path


def runtime_env():
    fixed={'PATH','HOME','USER','LOGNAME','SHELL','TERM','COLORTERM','LANG','TZ','TMPDIR','DISPLAY',
           'XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_CACHE_HOME','XDG_STATE_HOME','XDG_RUNTIME_DIR',
           'DBUS_SESSION_BUS_ADDRESS','HERMES_HOME','HERDR_CONFIG_PATH','CODEX_HOME','VIRTUAL_ENV',
           'SSL_CERT_FILE','SSL_CERT_DIR'}
    result={k:v for k,v in os.environ.items() if k in fixed or k.startswith('LC_')}
    result['PATH']=os.pathsep.join(p for p in os.get_exec_path() if p and trusted_path(p,directory=True))
    return result


def call(argv, *, cwd=None, data=None, timeout=35, acceptable=(0,)):
    env = runtime_env()
    env.update(GIT_TERMINAL_PROMPT='0',GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TEMPLATE_DIR=os.devnull,GIT_NO_REPLACE_OBJECTS='1',GIT_ASKPASS='/usr/bin/false',
               GH_PROMPT_DISABLED='1',GH_HOST='github.com',NO_COLOR='1')
    try:
        r = subprocess.run([trusted_executable(argv[0]),*argv[1:]], cwd=cwd, input=data, capture_output=True,
                           encoding='utf-8', timeout=timeout, env=env, check=False)
    except (OSError, subprocess.SubprocessError, UnicodeError) as e:
        raise Blocked('command-unavailable-or-timed-out') from e
    if r.returncode not in acceptable:
        raise Blocked('command-failed')
    return r.stdout


def obj(text):
    try:
        out = json.loads(text)
    except ValueError as e:
        raise Blocked('invalid-json') from e
    if not isinstance(out, dict):
        raise Blocked('invalid-json-object')
    return out


def wiki_url(repo):
    return f'https://github.com/{repo}.wiki.git'


def import_bundle(gitdir, handoff, expected):
    """Import an exact reviewed commit without entering the worker repository."""
    limit=256*1024**2
    copied=gitdir/'candidate.bundle'
    with os.fdopen(os.open(handoff,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=limit:
            raise Blocked('invalid-bundle-file')
        with copied.open('wb') as target:
            while chunk:=source.read(1024**2):
                if target.tell()+len(chunk)>limit: raise Blocked('bundle-too-large')
                target.write(chunk)
    def git(*argv):
        return call(['git','-C',str(gitdir),*argv],timeout=90).strip()
    if not SHA.fullmatch(expected) or git('bundle','list-heads',str(copied))!=f'{expected} HEAD':
        raise Blocked('bundle-does-not-match-reviewed-head')
    git('-c','fetch.fsckObjects=true','bundle','unbundle',str(copied))
    if git('cat-file','-t',expected)!='commit': raise Blocked('reviewed-head-is-not-commit')


class Identity:
    def __init__(self, repo, spec, signature, *, repo_id=None, reviewer_ids=()):
        if not REPO.fullmatch(repo):
            raise Blocked('invalid-repository')
        self.repo, self.spec, self.signature = repo, spec, signature
        self.repo_id = repo_id
        self.reviewer_ids = reviewer_ids
        self.verified = False

    def verify(self):
        if self.spec['type'] == 'app':
            value = obj(call([self.spec['launcher'], '--repo', self.repo, 'status']))
            ok = (value.get('authenticated') is True and value.get('repository','').lower() == self.repo.lower()
                  and value.get('actor') == self.spec['login'])
        else:
            ok = obj(call(['gh','api','user'])).get('login') == self.spec['login']
        if not ok:
            raise Blocked('publishing-identity-mismatch')
        self.verified = True

    def run(self, kind, args, **kw):
        if not self.verified:
            self.verify()
        if self.spec['type'] == 'app':
            prefix = [self.spec['launcher'],'--repo',self.repo,kind]
        elif kind == 'git':
            login = self.spec['login']
            prefix = ['git','-c','credential.helper=','-c','credential.https://github.com.helper=!gh auth git-credential',
                      '-c',f'user.name={login}','-c',f'user.email={login}@users.noreply.github.com']
        else:
            prefix = ['gh']
        return call([*prefix,*args], **kw)

    def git(self, args, **kw):
        # A scoped App launcher owns Git configuration; it rejects caller -c options.
        prefix=[] if self.spec['type']=='app' else ['-c','core.hooksPath=/dev/null','-c','http.followRedirects=false']
        return self.run('git',[*prefix,*args],**kw)

    def gh(self, args, **kw):
        return self.run('gh',args,**kw)

    def api(self, path):
        return obj(self.gh(['api',path]))

    def canonical_repo(self):
        if type(self.repo_id) is not int or self.repo_id<=0:
            raise Blocked('github-repo-id-not-configured')
        data=self.api(f'repos/{self.repo}')
        if (str(data.get('full_name','')).lower()!=self.repo or
                type(data.get('id')) is not int or data['id']!=self.repo_id):
            raise Blocked('github-repo-scope-mismatch')
        if str(data.get('clone_url','')).lower()!=f'https://github.com/{self.repo}.git':
            raise Blocked('github-clone-endpoint-mismatch')
        return data

    def ensure_label(self, label, description='Route this issue to the named Hermes project manager', *, before_write=None):
        from urllib.parse import quote
        path = f'repos/{self.repo}/labels/{quote(label, safe="")}'
        # Distinguish missing from transient failure by checking the full registry.
        def labels():
            return self.gh(['api','--paginate',f'repos/{self.repo}/labels?per_page=100','--jq','.[].name']).splitlines()
        if label in labels():
            return False
        self.canonical_repo()
        if before_write is not None:
            before_write()
        try:
            self.gh(['api','--method','POST',f'repos/{self.repo}/labels','-f',f'name={label}',
                     '-f','color=1d76db','-f',f'description={description[:100]}'])
        except Blocked:
            if label not in labels():
                raise
        if self.api(path).get('name') != label:
            raise Blocked('label-not-verified')
        return True

    def issues(self):
        text = self.gh(['api','--paginate',f'repos/{self.repo}/issues?state=open&per_page=100',
                        '--jq','.[] | select(has("pull_request") | not) | {number,title,labels:[.labels[].name]}'])
        rows = [obj(s) for s in text.splitlines() if s.strip()]
        if any(type(r.get('number')) is not int or not isinstance(r.get('title'),str) or not isinstance(r.get('labels'),list) for r in rows):
            raise Blocked('invalid-issue-list')
        return rows

    def issue_record(self, number):
        self.canonical_repo()
        repo_path=f'repos/{self.repo}'
        path=f'{repo_path}/issues/{number}'
        api_root='https://api.github.com/'
        row = self.api(path)
        if (type(row.get('number')) is not int or row['number']!=number or 'pull_request' in row or
                type(row.get('id')) is not int or row['id']<=0 or
                str(row.get('repository_url','')).lower()!=api_root+repo_path or
                str(row.get('url','')).lower()!=api_root+path):
            raise Blocked('issue-routing-unknown')
        return row

    def issue(self, number):
        row=self.issue_record(number)
        path=f'repos/{self.repo}/issues/{number}'
        text = self.gh(['api','--paginate',f'{path}/comments?per_page=100',
                        '--jq','.[] | {author:.user.login, body,updated_at,issue_url}'])
        comments=[obj(s) for s in text.splitlines() if s.strip()]
        if any(str(c.pop('issue_url','')).lower()!='https://api.github.com/'+path for c in comments):
            raise Blocked('issue-comment-scope-mismatch')
        self.canonical_repo()
        return row,comments

    def pr_text(self,issue,expected):
        link=re.fullmatch(re.escape(self.repo)+r'#([1-9][0-9]*)',issue)
        intro=f'Tracks #{link[1]}.\n\n' if link else ''
        title=f'Address issue #{link[1]}' if link else 'Apply reviewed project change'
        return title,f'{intro}Local review was checked for `{expected}`.\n\n{self.signature}'

    def push_pr(self, cwd, branch, base, issue, expected, prior=None, *, before_write, final_guard, resume,
                previous_pr=None, before_push=None, after_push=None, before_create=None):
        # Explicit host/repository selected from local config. Never push default branch.
        if not SHA.fullmatch(expected) or not re.fullmatch(r'hermes/[a-z0-9-]+/[a-f0-9]{16}',branch):
            raise Blocked('invalid-publish-scope')
        url = f'https://github.com/{self.repo}.git'
        ref = f'refs/heads/{branch}'
        def journal_attempt(record):
            if not callable(record):
                raise Blocked('publication-journal-required')
            final_guard()
            abort=record()
            if not callable(abort):
                raise Blocked('publication-journal-required')
            try:
                final_guard()
            except (Blocked,OSError,ValueError):
                abort()
                raise
        self.check_pr_target(branch,base)
        remote = self.git(['ls-remote',url,ref]).splitlines()
        exact=[f'{expected}\t{ref}']
        if resume and (cwd is not None or prior is not None or remote!=exact):
            raise Blocked('remote-publication-unverified')
        if not resume and remote==exact:
            raise Blocked('remote-branch-conflict')
        if (prior is None and previous_pr is not None) or (prior is not None and (type(previous_pr) is not int or previous_pr<=0)):
            raise Blocked('previous-publication-not-verified')
        # Reject an existing retargeted or foreign PR before touching its branch.
        if prior is not None and remote==[f'{prior}\t{ref}']:
            if self.existing_pr(branch,base,prior,require_open=True)!=previous_pr:
                raise Blocked('previous-publication-not-verified')
        elif not remote and self.existing_pr(branch,base,expected) is not None:
            raise Blocked('pr-ref-not-verified')
        if not resume:
            if prior is None:
                if remote:
                    raise Blocked('remote-branch-conflict')
            elif (not SHA.fullmatch(prior) or remote != [f'{prior}\t{ref}'] or
                  self.git(['-C',str(cwd),'merge-base',prior,expected]).strip()!=prior):
                raise Blocked('remote-branch-conflict')
            self.verify()
            before_write()
            if prior is not None and self.existing_pr(branch,base,prior,require_open=True)!=previous_pr:
                raise Blocked('previous-publication-not-verified')
            self.check_pr_target(branch,base)
            if not callable(after_push):
                raise Blocked('publication-journal-required')
            journal_attempt(before_push)
            result=self.git(['-C',str(cwd),'push','--porcelain',f'--force-with-lease={ref}:{prior or ""}',url,f'{expected}:{ref}'],timeout=90)
            updates=[line.split('\t') for line in result.splitlines() if '\t' in line]
            if (len(updates)!=1 or len(updates[0])!=3 or
                    updates[0][:2]!=['*' if prior is None else ' ',f'{expected}:{ref}']):
                raise Blocked('push-update-not-confirmed')
            # Journal the reported update even if the separate ref read fails.
            after_push()
        if self.git(['ls-remote',url,ref]).splitlines() != exact:
            raise Blocked('push-not-verified')
        number = self.existing_pr(branch,base,expected,require_open=True)
        if previous_pr is not None:
            if number!=previous_pr: raise Blocked('previous-publication-not-verified')
            if self.publication_ref_head(branch)!=expected: raise Blocked('remote-publication-unverified')
            return number
        if number is not None:
            raise Blocked('unexpected-existing-pr-inspect-before-recovery')
        self.verify()
        title,body = self.pr_text(issue,expected)
        before_write()
        if self.git(['ls-remote',url,ref]).splitlines()!=exact:
            raise Blocked('remote-publication-unverified')
        if self.existing_pr(branch,base,expected) is not None:
            raise Blocked('unexpected-existing-pr-inspect-before-recovery')
        self.check_pr_target(branch,base)
        journal_attempt(before_create)
        self.gh(['pr','create','-R',self.repo,'--base',base,'--head',branch,'--title',title,
                 '--body',body],timeout=60)
        number = self.existing_pr(branch,base,expected,require_open=True,expected_text=(title,body))
        if number is None: raise Blocked('pr-not-verified')
        if self.publication_ref_head(branch)!=expected: raise Blocked('remote-publication-unverified')
        return number

    def publication_ref_head(self,branch):
        if not re.fullmatch(r'hermes/[a-z0-9][a-z0-9-]{0,39}/[a-f0-9]{16}',branch):
            raise Blocked('invalid-publication-branch')
        path=f'repos/{self.repo}/git/matching-refs/heads/{branch}'
        try:
            matches=json.loads(self.gh(['api',path]))
        except ValueError as e:
            raise Blocked('remote-publication-unverified') from e
        if not isinstance(matches,list):
            raise Blocked('remote-publication-unverified')
        prefix=f'refs/heads/{branch}'
        url_prefix=f'https://api.github.com/repos/{self.repo.lower()}/git/'
        head=None
        seen=set()
        for row in matches:
            ref=row.get('ref') if isinstance(row,dict) else None
            if not isinstance(ref,str) or not ref.startswith(prefix) or ref in seen:
                raise Blocked('remote-publication-unverified')
            if (not re.fullmatch(r'refs/heads/[A-Za-z0-9._/-]+',ref) or '..' in ref or
                    any(not part or part.startswith('.') or part.endswith(('.', '.lock')) for part in ref.split('/'))):
                raise Blocked('remote-publication-unverified')
            obj=row.get('object'); url=row.get('url')
            if not isinstance(obj,dict):
                raise Blocked('remote-publication-unverified')
            sha=obj.get('sha')
            if (not isinstance(sha,str) or not SHA.fullmatch(sha) or
                    obj.get('type')!='commit' or not isinstance(url,str) or
                    url[:len(url_prefix)].lower()!=url_prefix or url[len(url_prefix):]!=ref):
                raise Blocked('remote-publication-unverified')
            seen.add(ref)
            if ref==prefix:
                head=sha
        return head

    def existing_pr(self,branch,base,expected,require_open=False,expected_text=None):
        text = self.gh(['pr','list','-R',self.repo,'--state','all','--head',branch,'--limit','40',
                        '--json','number,headRefOid,baseRefName,state,isCrossRepository'])
        matches = json.loads(text)
        if matches==[]: return None
        if (not isinstance(matches,list) or len(matches)!=1 or not isinstance(matches[0],dict) or
                type(matches[0].get('number')) is not int or matches[0]['number']<=0):
            raise Blocked('pr-scope-ambiguous')
        number = matches[0]['number']
        record = self.api(f'repos/{self.repo}/pulls/{number}')
        self.verify_pr_route(record,base,branch,publication=True)
        if require_open and record.get('state')!='open':
            raise Blocked('previous-pr-is-no-longer-open')
        if (record.get('number')!=number or record.get('head',{}).get('sha') != expected
                or record.get('user',{}).get('login') != self.spec['login']):
            raise Blocked('pr-actor-or-head-mismatch')
        if expected_text is not None and (record.get('title'),record.get('body'))!=expected_text:
            raise Blocked('pr-text-mismatch')
        return number

    def check_pr_target(self,branch,base):
        repo=self.canonical_repo()
        # The signed publication route is public-only. Do not ignore a newer
        # observation that contradicts the audience in the signed request.
        if repo.get('visibility')!='public' or repo.get('private') is not False:
            raise Blocked('publication-target-audience-unverified')
        default=repo.get('default_branch')
        if not isinstance(default,str) or not default or branch in (default,base):
            raise Blocked('invalid-live-publication-target')

    def verify_pr_route(self,p,base,branch,*,publication=False):
        if publication:
            self.check_pr_target(branch,base)
        else:
            self.canonical_repo()
        for key,value in (('base',base),('head',branch)):
            side=p.get(key) or {}
            repo=side.get('repo') or {}
            if (side.get('ref')!=value or str(repo.get('full_name','')).lower()!=self.repo or
                    type(repo.get('id')) is not int or repo['id']!=self.repo_id):
                raise Blocked('tracked-pr-route-changed')
            if publication and (repo.get('private',False) is not False or repo.get('visibility','public')!='public'):
                # Embedded records may omit visibility, but contradictory evidence is a hold.
                raise Blocked('publication-target-audience-unverified')

    def pull_state(self, number, *, base=None, branch=None, required=None):
        p = self.api(f'repos/{self.repo}/pulls/{number}')
        if base is not None or branch is not None:
            self.verify_pr_route(p,base,branch)
        head = (p.get('head') or {}).get('sha')
        if not isinstance(head,str) or not SHA.fullmatch(head) or p.get('number') != number:
            raise Blocked('pr-head-unknown')
        if p.get('merged') is True or p.get('state')=='closed':
            return {'head':head,'state':'merged' if p.get('merged') is True else 'closed','checks':'missing'}
        return {'head':head,'state':'working','checks':self.check_state(head,required,branch=branch),
                'draft':p.get('draft') is not False}

    def check_state(self, head, required, *, branch=None, main_push=False):
        if not SHA.fullmatch(head): raise Blocked('ci-scope-mismatch')
        combined = self.api(f'repos/{self.repo}/commits/{head}/status')
        runs = self.gh(['api','--paginate',f'repos/{self.repo}/commits/{head}/check-runs?filter=latest&per_page=100',
                        '--jq','.check_runs[] | {id,name,head_sha,status,conclusion,suite_id:.check_suite.id,app_slug:.app.slug}'])
        rows = [obj(s) for s in runs.splitlines() if s.strip()]
        if any(r.get('head_sha')!=head for r in rows) or combined.get('sha')!=head:
            raise Blocked('ci-scope-mismatch')
        states = [r.get('conclusion') if r.get('status')=='completed' else 'pending' for r in rows]
        if combined.get('total_count'):
            states.append(combined.get('state'))
        if any(x not in ('pending','success','neutral','skipped') for x in states):
            checks='fail'
        elif any(x in ('pending','neutral','skipped') for x in states):
            checks='pending'
        elif not self.trusted_checks(head,rows,required,branch,main_push):
            checks='missing'
        elif 'success' in states:
            checks='pass'
        else:
            checks='missing'
        return checks

    def trusted_checks(self,head,checks,policy,branch,main_push):
        if not isinstance(policy,dict) or not policy or type(self.repo_id) is not int:
            return False
        record=self.api(f'repos/{self.repo}/actions/runs?head_sha={head}&per_page=100')
        runs=record.get('workflow_runs',[])
        if type(record.get('total_count')) is not int or not isinstance(runs,list) or record['total_count']!=len(runs):
            return False
        confirmed=set()
        attempt_jobs={}
        for name,pin in policy.items():
            path,blob,workflow_id=pin['path'],pin['blob'],pin['workflow_id']
            matching=[r for r in runs if r.get('workflow_id')==workflow_id
                      and r.get('head_sha')==head and (r.get('repository') or {}).get('id')==self.repo_id
                      and (r.get('head_repository') or {}).get('id')==self.repo_id
                      and r.get('event')=='push'
                      and (branch is None or r.get('head_branch')==branch)
                      and (not main_push or r.get('head_branch')=='main')]
            sequence=[r.get('run_number') for r in matching]
            if (not sequence or any(type(n) is not int or n<=0 for n in sequence)
                    or len(set(sequence))!=len(sequence)):
                return False
            run=max(matching,key=lambda r:r['run_number'])
            if (run.get('path'),run.get('status'),run.get('conclusion'))!=(path,'completed','success'):
                return False
            run_id=run.get('id'); attempt=run.get('run_attempt'); suite=run.get('check_suite_id')
            if any(type(n) is not int or n<=0 for n in (run_id,attempt,suite)):
                return False
            if (run_id,attempt) not in attempt_jobs:
                data=self.api(f'repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
                jobs=data.get('jobs')
                if not isinstance(jobs,list) or type(data.get('total_count')) is not int or data['total_count']!=len(jobs):
                    return False
                attempt_jobs[run_id,attempt]=jobs
            jobs=[j for j in attempt_jobs[run_id,attempt] if j.get('name')==name]
            if len(jobs)!=1 or any(jobs[0].get(k)!=v for k,v in {
                    'run_id':run_id,'head_sha':head,
                    'head_branch':run.get('head_branch'),'status':'completed','conclusion':'success'}.items()):
                return False
            # The endpoint identifies the attempt. Some responses omit the
            # redundant per-job field, but conflicting values are invalid.
            if 'run_attempt' in jobs[0] and jobs[0]['run_attempt']!=attempt:
                return False
            check_url=jobs[0].get('check_run_url')
            candidates=[c for c in checks if c.get('suite_id')==suite and c.get('name')==name and
                        c.get('status')=='completed' and c.get('conclusion')=='success' and
                        c.get('app_slug')=='github-actions' and type(c.get('id')) is int and c['id']>0 and
                        isinstance(check_url,str) and re.fullmatch(
                            rf'https://api\.github\.com/repos/(?i:{re.escape(self.repo)})/check-runs/{c["id"]}',check_url)]
            if len(candidates)!=1:
                return False
            if (path,blob) not in confirmed:
                if not WORKFLOW.fullmatch(path) or not SHA.fullmatch(blob): return False
                file=self.api(f'repos/{self.repo}/contents/{path}?ref={head}')
                if (file.get('type'),file.get('path'),file.get('sha'))!=('file',path,blob):
                    return False
                confirmed.add((path,blob))
        return True


def publish_wiki(git, remote, page, contents, scratch, before_push=None):
    """Update one managed page on latest wiki head, non-force; verify remote bytes."""
    if not re.fullmatch(r'Manager-[a-z0-9-]+(?:-Log)?\.md',page):
        raise Blocked('invalid-wiki-page')
    with tempfile.TemporaryDirectory(prefix='wiki-',dir=scratch) as tmp:
        work = Path(tmp) / 'checkout'
        git(['clone','--quiet','--',remote,str(work)],timeout=90)
        call(['git','-C',str(work),'config','--local','core.hooksPath',os.devnull])
        branch = git(['-C',str(work),'symbolic-ref','--quiet','--short','HEAD']).strip()
        head = git(['-C',str(work),'rev-parse','HEAD']).strip()
        if not SHA.fullmatch(head) or not re.fullmatch(r'[\w.-]+',branch):
            raise Blocked('wiki-not-initialized')
        path = work / page
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise Blocked('unsafe-wiki-page')
        expected = f'{head}\trefs/heads/{branch}'
        def readref():
            return git(['ls-remote',remote,f'refs/heads/{branch}']).strip()
        if readref() != expected:
            raise Blocked('concurrent-wiki-update')
        if path.is_file() and path.read_bytes() == contents.encode():
            return {'status':'unchanged','sha':head}
        path.write_bytes(contents.encode())
        git(['-C',str(work),'add','--',page])
        if git(['-C',str(work),'diff','--cached','--name-only']).splitlines() != [page]:
            raise Blocked('wiki-write-escaped-scope')
        git(['-C',str(work),'commit','-m',f'Update {page[:-3]}'])
        sha = git(['-C',str(work),'rev-parse','HEAD']).strip()
        if git(['-C',str(work),'show',f'{sha}:{page}']) != contents:
            raise Blocked('wiki-commit-content-changed-before-push')
        if before_push: before_push()
        target=f'refs/heads/{branch}'
        if git(['-C',str(work),'merge-base',head,sha]).strip()!=head:
            raise Blocked('wiki-update-is-not-fast-forward')
        git(['-C',str(work),'push',f'--force-with-lease={target}:{head}',remote,f'{sha}:{target}'],timeout=90)
        if readref() != f'{sha}\trefs/heads/{branch}':
            raise Blocked('wiki-write-not-verified')
        if git(['-C',str(work),'show',f'{sha}:{page}']) != contents:
            raise Blocked('wiki-content-not-verified')
        return {'status':'published','sha':sha}
