"""Small GitHub CLI gateway. Credentials remain in gh/environment, never plans."""
import json
import subprocess
import time
from urllib.parse import quote
from .processes import owned


class GitHub:
    def __init__(self, repository, command=('gh',)):
        self.repository, self.command = repository, list(command)

    def call(self, args, payload=None, absent=False):
        with owned([*self.command, *args], stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8') as result:
            stdout, stderr = result.communicate(json.dumps(payload) if payload is not None else None, timeout=900)
        if result.returncode:
            if absent and 'HTTP 404' in stderr:
                return None
            # gh errors may contain environment-specific/private endpoint data.
            raise RuntimeError('GitHub operation failed; reconcile remote state before retrying')
        return json.loads(stdout) if stdout.strip() else None

    def api(self, path, method='GET', payload=None, absent=False):
        args = ['api', 'repos/' + self.repository + '/' + path, '--method', method]
        if payload is not None:
            args += ['--input', '-']
        return self.call(args, payload, absent)

    def pages(self, path, field=None):
        result = []
        for page in range(1, 101):
            rows = self.api(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            rows = rows[field] if field else rows
            if not isinstance(rows, list):
                raise ValueError('Invalid GitHub page')
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise ValueError('GitHub pagination limit exceeded')

    def ci(self, declaration, release):
        run_id = declaration['run_id']
        run = self.api(f'actions/runs/{run_id}')
        if (run['head_sha'] != release['commit'] or run['path'] != '.github/workflows/' + declaration['workflow']
                or run['status'] != 'completed' or run['conclusion'] != 'success'
                or run['repository']['full_name'].lower() != self.repository.lower()):
            raise ValueError('CI is pending, failed, or belongs to another source/workflow/repository')
        attempt = run['run_attempt']
        jobs = self.pages(f'actions/runs/{run_id}/attempts/{attempt}/jobs', 'jobs')
        for name in declaration['required_jobs']:
            matches = [j for j in jobs if j['name'] == name]
            if len(matches) != 1 or matches[0]['conclusion'] != 'success':
                raise ValueError('Required CI job did not succeed exactly once: ' + name)
        return {'run_id': run_id, 'attempt': attempt, 'commit': run['head_sha'],
                'workflow': run['path'], 'required_jobs': declaration['required_jobs']}

    def source(self, release):
        value = self.api('git/commits/' + release['commit'])
        if value['sha'] != release['commit'] or value['tree']['sha'] != release['tree']:
            raise ValueError('Remote source commit/tree mismatch')

    def tag(self, tag):
        row = self.api('git/ref/tags/' + quote(tag, safe=''), absent=True)
        if row is None:
            return None
        obj = row['object']
        for _ in range(8):
            if obj['type'] == 'commit':
                return obj['sha']
            if obj['type'] != 'tag':
                break
            obj = self.api('git/tags/' + obj['sha'])['object']
        raise ValueError('Unsupported or cyclic remote tag')

    def release(self, tag):
        found = [r for r in self.pages('releases') if r['tag_name'] == tag]
        if len(found) > 1:
            raise ValueError('Duplicate releases for fixed tag')
        return found[0] if found else None

    def assets(self, release):
        rows = self.pages(f'releases/{release["id"]}/assets')
        if len({r['name'] for r in rows}) != len(rows):
            raise ValueError('Duplicate remote asset names')
        return {r['name']: r for r in rows}

    def upload(self, tag, path):
        # No --clobber: a concurrent asset can never be silently overwritten.
        return self.call(['release', 'upload', tag, str(path), '--repo', self.repository])

    def download_artifact(self, artifact_id, output, maximum):
        # The authenticated official CLI performs the API download. Neither its
        # credentials nor signed redirect URLs are copied into local evidence.
        if output.is_symlink():
            raise ValueError('Redirected artifact download destination')
        with output.open('wb') as stream, owned([*self.command, 'api',
                f'repos/{self.repository}/actions/artifacts/{artifact_id}/zip'],
                stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.DEVNULL) as process:
            deadline = time.monotonic() + 900
            while process.poll() is None:
                if output.stat().st_size > maximum or time.monotonic() > deadline:
                    raise ValueError('CI artifact download exceeded its size/deadline')
                time.sleep(.1)
        if process.returncode or output.stat().st_size > maximum:
            raise ValueError('CI artifact download failed')
