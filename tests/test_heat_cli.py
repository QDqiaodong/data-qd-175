"""CLI tests for ``loan-heat`` via independent Python subprocesses."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
T0 = "2026-06-24T10:00:00Z"


def _run_cli(*args, stdin=None):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "museum.heat_cli", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, input=stdin)


def _source():
    return {"as_of": "2026-06-30",
            "loans": [{"id": "L1", "object_id": "OBJ-1", "borrower": "b",
                       "start": "2026-06-01", "due": "2026-06-20"}],
            "events": []}


def _manifest():
    return {
        "start": T0, "end": "2026-06-24T10:00:30Z",
        "probes": [
            {"probe_id": "P1", "hold_seconds": 10,
             "calibrations": [{"start": "2026-06-24T09:00:00Z",
                               "end": "2026-06-25T00:00:00Z",
                               "offset_milli": 0}]},
            {"probe_id": "P2", "hold_seconds": 10,
             "calibrations": [{"start": "2026-06-24T09:00:00Z",
                               "end": "2026-06-25T00:00:00Z",
                               "offset_milli": 0}]}],
        "limits": [{"loan_id": "L1", "ceiling_milli": 20000,
                    "alarm_seconds": 10}],
        "placements": [{"loan_id": "L1", "start": T0,
                        "end": "2026-06-24T10:00:30Z",
                        "probe_ids": ["P1", "P2"]}]}


def _rows(value=21000):
    return "".join(
        json.dumps({"probe_id": probe,
                    "at": f"2026-06-24T10:00:{second:02d}Z",
                    "reading_milli": value}) + "\n"
        for second in (0, 10, 20) for probe in ("P1", "P2"))


def _row_usable(result):
    return {row["probe_id"]: row for row in result["probe_health"]}["P1"][
        "usable"]


class CliTests(unittest.TestCase):
    def _write(self, directory, name, data):
        path = Path(directory) / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _paths(self, tmp):
        return (self._write(tmp, "source.json", _source()),
                self._write(tmp, "manifest.json", _manifest()))

    def test_success_from_file_and_stdin_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self._paths(tmp)
            samples = Path(tmp) / "samples.ndjson"
            samples.write_text(_rows(), encoding="utf-8")
            scratch = Path(tmp) / "scratch"
            output_file = Path(tmp) / "from-file.json"
            output_pipe = Path(tmp) / "from-pipe.json"

            first = _run_cli("--source", str(source), "--manifest",
                             str(manifest), "--samples", str(samples),
                             "--scratch", str(scratch),
                             "--output", str(output_file))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout),
                             {"loans": 1, "isolate": 1, "review": 0,
                              "release": 0, "probes": 2})

            second = _run_cli("--source", str(source), "--manifest",
                              str(manifest), "--samples", "-",
                              "--scratch", str(scratch),
                              "--output", str(output_pipe), stdin=_rows())
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(output_file.read_text(),
                             output_pipe.read_text())
            result = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(result["loans"][0]["decision"], "isolate")
            self.assertEqual(result["loans"][0]["longest_hot_seconds"], 30)
            self.assertEqual(
                sorted(result["probe_health"][0]),
                ["collision", "fault", "probe_id", "stale", "uncalibrated",
                 "usable"])
            # Per-run scratch is cleaned up and nothing else is touched.
            self.assertTrue(scratch.is_dir())
            self.assertEqual(list(scratch.iterdir()), [])

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self._paths(tmp)
            output = Path(tmp) / "result.json"
            output.write_text("keep me\n", encoding="utf-8")
            completed = _run_cli("--source", str(source), "--manifest",
                                 str(manifest), "--samples", "-",
                                 "--scratch", str(tmp),
                                 "--output", str(output), stdin=_rows())
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-heat: "))
            self.assertIn("already exists", completed.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me\n")

    def test_trailing_bad_line_fails_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self._paths(tmp)
            samples = Path(tmp) / "samples.ndjson"
            samples.write_text(_rows() + '{"probe_id": "P1",\n',
                               encoding="utf-8")
            output = Path(tmp) / "result.json"
            completed = _run_cli("--source", str(source), "--manifest",
                                 str(manifest), "--samples", str(samples),
                                 "--scratch", str(tmp),
                                 "--output", str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-heat: "))
            self.assertIn("line 7", completed.stderr)
            self.assertFalse(output.exists())
            self.assertEqual(
                [p.name for p in Path(tmp).iterdir()
                 if p.name not in {"source.json", "manifest.json",
                                   "samples.ndjson"}], [])

    def test_invalid_manifest_reports_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self._paths(tmp)
            broken = _manifest()
            broken["placements"][0]["probe_ids"] = ["P1"]
            bad = self._write(tmp, "bad.json", broken)
            output = Path(tmp) / "result.json"
            completed = _run_cli("--source", str(source), "--manifest",
                                 str(bad), "--samples", "-",
                                 "--scratch", str(tmp),
                                 "--output", str(output), stdin="")
            self.assertEqual(completed.returncode, 2)
            self.assertIn("placements[0]", completed.stderr)
            self.assertFalse(output.exists())

    def test_missing_source_is_a_reported_path_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            _, manifest = self._paths(tmp)
            completed = _run_cli("--source", str(missing), "--manifest",
                                 str(manifest), "--samples", "-",
                                 "--scratch", str(tmp),
                                 "--output", str(Path(tmp) / "r.json"),
                                 stdin="")
            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("loan-heat: "))
            self.assertIn("source", completed.stderr)

    def test_invalid_utf8_names_the_offending_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self._paths(tmp)
            samples = Path(tmp) / "samples.ndjson"
            samples.write_bytes(
                _rows().encode("utf-8").split(b"\n", 1)[0] + b"\n"
                + b"\xff\xfe\n")
            output = Path(tmp) / "result.json"
            completed = _run_cli("--source", str(source), "--manifest",
                                 str(manifest), "--samples", str(samples),
                                 "--scratch", str(tmp), "--output",
                                 str(output))
            self.assertEqual(completed.returncode, 2)
            self.assertIn("line 2", completed.stderr)
            self.assertIn("UTF-8", completed.stderr)
            self.assertFalse(output.exists())

    def test_failed_sample_stream_cleans_its_scratch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self._paths(tmp)
            scratch = Path(tmp) / "scratch"
            scratch.mkdir()
            (scratch / "someone-else.txt").write_text("keep\n",
                                                      encoding="utf-8")
            output = Path(tmp) / "result.json"
            completed = _run_cli("--source", str(source), "--manifest",
                                 str(manifest), "--samples", "-",
                                 "--scratch", str(scratch),
                                 "--output", str(output),
                                 stdin=_rows() + "not-json\n")
            self.assertEqual(completed.returncode, 2)
            self.assertFalse(output.exists())
            self.assertEqual(sorted(p.name for p in scratch.iterdir()),
                             ["someone-else.txt"])

    def test_stdin_is_consumed_once(self):
        # A pipe that yields the same data twice would change the result if
        # it were re-read; a single read makes this a plain success.
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self._paths(tmp)
            output = Path(tmp) / "result.json"
            completed = _run_cli("--source", str(source), "--manifest",
                                 str(manifest), "--samples", "-",
                                 "--scratch", str(tmp), "--output",
                                 str(output), stdin=_rows())
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(_row_usable(result), 30)

    def test_scratch_is_reusable_and_run_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self._paths(tmp)
            scratch = Path(tmp) / "shared"
            scratch.mkdir()
            (scratch / "unrelated").write_text("x", encoding="utf-8")
            for name in ("a.json", "b.json"):
                completed = _run_cli(
                    "--source", str(source), "--manifest", str(manifest),
                    "--samples", "-", "--scratch", str(scratch),
                    "--output", str(Path(tmp) / name), stdin=_rows())
                self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(sorted(p.name for p in scratch.iterdir()),
                             ["unrelated"])

    def test_scratch_directory_can_be_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, manifest = self._paths(tmp)
            output = Path(tmp) / "nested" / "result.json"
            completed = _run_cli("--source", str(source), "--manifest",
                                 str(manifest), "--samples", "-",
                                 "--output", str(output), stdin=_rows())
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
