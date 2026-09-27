import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class IngestConfigTests(unittest.TestCase):
    def read_config(self, overrides=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            shutil.copyfile(
                Path(__file__).resolve().parents[1] / "ingest_graph.py",
                project / "ingest_graph.py",
            )
            (project / ".env").write_text(
                "NEO4J_URI=bolt://example.invalid:7687\n"
                "NEO4J_USER=fixture-user\n"
                "NEO4J_PASSWORD=fixture-password\n",
                encoding="utf-8",
            )
            env = {k: v for k, v in os.environ.items() if not k.startswith("NEO4J_")}
            env["PYTHONPATH"] = str(project)
            env.update(overrides or {})
            result = subprocess.run(
                [sys.executable, "-c", "import json, ingest_graph as g; "
                 "print(json.dumps([g.NEO4J_URI, g.NEO4J_USER, g.NEO4J_PASSWORD]))"],
                cwd=root,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(result.stdout)

    def test_loads_env_beside_script_from_another_working_directory(self):
        self.assertEqual(self.read_config(), [
            "bolt://example.invalid:7687", "fixture-user", "fixture-password",
        ])

    def test_exported_environment_takes_precedence(self):
        self.assertEqual(self.read_config({"NEO4J_PASSWORD": "exported-password"}), [
            "bolt://example.invalid:7687", "fixture-user", "exported-password",
        ])


if __name__ == "__main__":
    unittest.main()
