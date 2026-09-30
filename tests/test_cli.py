import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from railcan.cli import main
from railcan.traces import read_trace


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, args):
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            code = main(args)
        return code, out.getvalue(), error.getvalue()

    def test_generate_inspect_and_convert(self):
        source, dest = self.root / "run.log", self.root / "run.csv"
        self.assertEqual(self.invoke(["simulate", "--duration", "1", "--out", str(source)])[0], 0)
        self.assertEqual(len(list(read_trace(source))), 62)
        code, output, _ = self.invoke(["inspect", str(source), "--json"])
        self.assertEqual(code, 0)
        self.assertIn('"frames": 62', output)
        self.assertEqual(self.invoke(["replay", str(source), "--out", str(dest)])[0], 0)
        self.assertEqual(list(read_trace(source)), list(read_trace(dest)))

    def test_profile_validate_and_dbc(self):
        profile, dbc = self.root / "train.json", self.root / "train.dbc"
        self.assertEqual(self.invoke(["profile", "--out", str(profile)])[0], 0)
        self.assertEqual(self.invoke(["validate", str(profile)])[0], 0)
        self.assertEqual(self.invoke(["dbc", "--profile", str(profile), "--out", str(dbc)])[0], 0)
        self.assertIn("BO_ 256 Motion", dbc.read_text())

    def test_replay_does_not_overwrite_input(self):
        source = self.root / "run.log"
        self.invoke(["simulate", "--duration", "1", "--out", str(source)])
        original = source.read_bytes()
        code, _, error = self.invoke(["replay", str(source), "--out", str(source)])
        self.assertEqual(code, 1)
        self.assertIn("differ", error)
        self.assertEqual(original, source.read_bytes())

    def test_invalid_values_fail_cleanly(self):
        for args in [["simulate", "--duration", "nan"], ["simulate", "--rate", "0"],
                     ["simulate", "--duration", "1", "--format", "pcap"],
                     ["validate", str(self.root / "missing.json")]]:
            code, _, error = self.invoke(args)
            self.assertEqual(code, 1)
            self.assertTrue(error.startswith("RailCAN:"))

    def test_stdout_trace_has_summary_on_stderr(self):
        code, output, error = self.invoke(["simulate", "--duration", ".1"])
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith("(0.000000) vcan0 100#"))
        self.assertIn("Generated", error)
        self.assertNotIn("Generated", output)
