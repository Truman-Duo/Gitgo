"""Merge-gate regressions; no project runtime or credentials are required."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

spec = importlib.util.spec_from_file_location("ci_contract", Path(__file__).parents[1] / "scripts/check_pr_contract.py")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)

CONFIG = {"projectPrefix": "GITGO", "validTypes": ["fix", "chore"],
          "validScopes": ["security", "config"], "maxSubjectLength": 60}


def candidate():
    return {"title": "fix(security): enforce the boundary", "head": {"sha": "a" * 40,
        "ref": "fix/sandbox", "repo": {"full_name": "contributor/Gitgo"}},
        "base": {"sha": "b" * 40, "ref": "master", "repo": {"full_name": "Truman-Duo/Gitgo"}},
        "body": "\n".join(["## 问题与目标", "Contain tool effects.",
        "## 协作范围与版本", "- 候选 head SHA：`" + "a" * 40 + "`",
        "- 验证时的目标 base SHA：`" + "b" * 40 + "`",
        "- 源仓库与功能分支：`contributor/Gitgo:fix/sandbox`",
        "- 目标仓库与分支：`Truman-Duo/Gitgo:master`",
        "## 设计与权威边界", "Host owns policy.", "## 验证", "CI pending; local gate tests passed.",
        "## 兼容、迁移与回滚", "No schema changes.", "## 上下游与生命周期", "Retest on upstream changes.",
        "## 未解决风险", "Native/UI acceptance remains separate."])}


class ContractTests(unittest.TestCase):
    def test_valid_plain_contribution(self):
        self.assertEqual(contract.validate_pr(candidate(), CONFIG), [])

    def test_old_sha_in_history_does_not_validate_latest_candidate(self):
        pr = candidate()
        pr["head"]["sha"] = "c" * 40
        pr["body"] += "\nHistorical success for " + "c" * 40
        self.assertTrue(any("head SHA" in error for error in contract.validate_pr(pr, CONFIG)))

    def test_upstream_changes_invalidate_declared_base(self):
        pr = candidate()
        pr["base"]["sha"] = "c" * 40
        self.assertTrue(any("target SHA" in error for error in contract.validate_pr(pr, CONFIG)))

    def test_fork_default_branch_is_not_the_target(self):
        pr = candidate()
        pr["body"] = pr["body"].replace("Truman-Duo/Gitgo:master", "contributor/Gitgo:master")
        self.assertTrue(any("target field" in error for error in contract.validate_pr(pr, CONFIG)))

    def test_template_comments_do_not_count_as_evidence(self):
        pr = candidate()
        pr["body"] = pr["body"].replace("Host owns policy.", "<!-- TODO -->")
        self.assertTrue(any("设计" in error for error in contract.validate_pr(pr, CONFIG)))

    def test_invalid_scope_and_subject(self):
        self.assertTrue(contract.check_title("fix(other): wrong.", CONFIG))
        self.assertTrue(contract.check_title("fix(security): " + "s" * 61, CONFIG))

    def test_formal_number_only_required_at_integration(self):
        self.assertEqual(contract.check_title("chore(config): set up CI", CONFIG), [])
        self.assertTrue(contract.check_title("chore(config): set up CI", CONFIG, formal=True))
        self.assertEqual(contract.check_title("[GITGO-75] chore(config): set up CI", CONFIG, formal=True), [])

    def test_native_acceptance_cannot_disappear_with_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            root, base = Path(temp) / "root", Path(temp) / "base"
            root.mkdir()
            (base / "backend/core").mkdir(parents=True)
            (base / "backend/core/sandbox.py").touch()
            with self.assertRaises(ValueError):
                contract.native_required(root, base)

    def test_baseline_without_native_backend_does_not_claim_native_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(contract.native_required(Path(temp), Path(temp)))


class WindowsPreviewGateTests(unittest.TestCase):
    def run_gate(self, changes=None):
        required = ['contract', 'privacy', 'python-windows', 'dashboard', 'runtime',
                    'windows-package', 'managed-update-windows', 'native']
        jobs = {name: {'result': 'success'} for name in required}
        jobs['contract']['outputs'] = {'native_required': 'true'}
        for name in ['native-linux', 'unix-packages', 'managed-update-linux']:
            jobs[name] = {'result': 'failure'}
        for name, result in (changes or {}).items():
            jobs[name]['result'] = result
        workflow = (Path(__file__).parents[1] / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        script = textwrap.dedent(workflow.rsplit("python3 -I - <<'PY'\n", 1)[1]).rsplit('\nPY', 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            summary = Path(temp) / 'summary.md'
            result = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True,
                text=True, env=dict(os.environ, RESULTS=json.dumps(jobs), GITHUB_STEP_SUMMARY=str(summary)))
            return result, summary.read_text(encoding='utf-8')

    def test_diagnostic_failures_are_visible_without_claiming_platform_support(self):
        result, summary = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('unsupported release targets', summary)
        self.assertIn('unix-packages: failure', summary)

    def test_every_windows_requirement_fails_closed(self):
        for name in ['contract', 'privacy', 'python-windows', 'dashboard', 'runtime',
                     'windows-package', 'managed-update-windows', 'native']:
            for status in ['failure', 'cancelled', 'skipped']:
                with self.subTest(name=name, status=status):
                    result, _ = self.run_gate({name: status})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)


if __name__ == "__main__":
    unittest.main()
