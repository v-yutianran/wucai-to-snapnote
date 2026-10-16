import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class ScheduleTests(unittest.TestCase):
    def test_environment_loads_without_running_interactive_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".zshenv").write_text("export SNAPNOTE_MCP_TOKEN=synthetic-snapnote\n")
            (root / ".zprofile").write_text("")
            (root / ".zlogin").write_text("")
            (root / ".zshrc").write_text(
                "export WUCAI_TOKEN=wct-synthetic\nprint -u2 INTERACTIVE-PROMPT-MUST-NOT-RUN\n")
            stub = root / "backup.py"
            stub.write_text(
                "import json,os,sys\n"
                "assert os.environ['WUCAI_TOKEN']==os.environ['EXPECTED_WUCAI_TOKEN']\n"
                "assert os.environ['SNAPNOTE_MCP_TOKEN']=='synthetic-snapnote'\n"
                "print(json.dumps(sys.argv[1:]))\n")
            source = Path(__file__).resolve().parents[1] / "run-scheduled.zsh"
            wrapper = root / "run.zsh"
            wrapper.write_text(source.read_text().replace(
                "/Users/xinwu/project/wucai-to-snapnote/backup.py", str(stub)))
            for inherited in (False, True):
                with self.subTest(inherited=inherited):
                    env = {key:value for key,value in os.environ.items()
                           if key not in ("WUCAI_TOKEN", "SNAPNOTE_MCP_TOKEN")}
                    env.update(ZDOTDIR=str(root), EXPECTED_WUCAI_TOKEN="wct-inherited" if inherited else "wct-synthetic")
                    if inherited:
                        env["WUCAI_TOKEN"] = "wct-inherited"
                    result = subprocess.run(["/bin/zsh", str(wrapper), "--dry-run", "argument with spaces"],
                                            env=env, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(json.loads(result.stdout), ["--dry-run", "argument with spaces"])
