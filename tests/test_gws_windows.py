"""gws launcher resolution and argument passing on Windows (PowerShell 5.1 quoting), with mocks.

No Google credentials are used and nothing is written to any sheet: the runner is faked, except one
round trip through a local `node -e` that only echoes its arguments.
"""

import json
import os
import shutil
import subprocess
import unittest

import sheets_tracker as st

NPM = r"C:\Users\someone\AppData\Roaming\npm"
RUN_JS = os.path.join(NPM, "node_modules", "@googleworkspace", "cli", "run.js")
NODE = r"C:\Program Files\nodejs\node.exe"
PARAMS = json.dumps({"spreadsheetId": "sheet-123", "range": "Sheet1!A:L"})


def fake_which(found):
    return lambda name: found.get(name)


def fake_exists(paths):
    return lambda path: path in paths


class TestResolveGwsCommand(unittest.TestCase):
    def resolve(self, found=None, files=(), environ=None, os_name="nt"):
        return st.resolve_gws_command(which=fake_which(found or {}), exists=fake_exists(set(files)),
                                      environ=environ or {}, os_name=os_name)

    def test_posix_is_unchanged(self):
        self.assertEqual(self.resolve({"gws.cmd": NPM + r"\gws.cmd"}, {RUN_JS}, os_name="posix"), ["gws"])

    def test_windows_calls_run_js_with_node_not_a_shim(self):
        command = self.resolve({"gws.cmd": NPM + r"\gws.cmd", "node": NODE}, {RUN_JS})
        self.assertEqual(command, [NODE, RUN_JS])

    def test_prefers_the_node_exe_next_to_the_shim(self):
        local_node = os.path.join(NPM, "node.exe")
        self.assertEqual(self.resolve({"gws.cmd": NPM + r"\gws.cmd", "node": NODE}, {RUN_JS, local_node}),
                         [local_node, RUN_JS])

    def test_found_from_the_powershell_shim_but_never_runs_it(self):
        command = self.resolve({"gws.ps1": NPM + r"\gws.ps1", "node": NODE}, {RUN_JS})
        self.assertEqual(command, [NODE, RUN_JS])
        self.assertFalse(any(c.lower().endswith(".ps1") for c in command))

    def test_found_from_appdata_when_path_has_no_shim(self):
        appdata = r"C:\Users\other\AppData\Roaming"
        run_js = os.path.join(appdata, "npm", "node_modules", "@googleworkspace", "cli", "run.js")
        self.assertEqual(self.resolve({"node": NODE}, {run_js}, environ={"APPDATA": appdata}), [NODE, run_js])

    def test_explicit_cmd_shim_only_when_run_js_is_missing(self):
        self.assertEqual(self.resolve({"gws.cmd": NPM + r"\gws.cmd", "node": NODE}, set()), [NPM + r"\gws.cmd"])

    def test_no_node_falls_back_to_the_cmd_shim(self):
        self.assertEqual(self.resolve({"gws.cmd": NPM + r"\gws.cmd"}, {RUN_JS}), [NPM + r"\gws.cmd"])

    def test_not_installed_is_never_a_bare_gws(self):
        self.assertEqual(self.resolve({}, set()), ["gws.cmd"])


class Recorder:
    def __init__(self, returncode=0, stdout="{}", stderr="", raises=None):
        self.calls, self.returncode, self.stdout, self.stderr, self.raises = [], returncode, stdout, stderr, raises

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


class TestArgumentPassing(unittest.TestCase):
    def test_json_is_one_untouched_list_element_and_no_shell(self):
        runner = Recorder(stdout='{"values": [["Job Title"]]}')
        client = st.GwsClient([NODE, RUN_JS], runner=runner)
        values = json.dumps({"values": [['AT&T "Labs" <Pune> 50% ^x | ü', "Sheet1!A:L"]]})
        self.assertEqual(client("sheets", "spreadsheets", "values", "get", "--params", PARAMS, "--json", values),
                         {"values": [["Job Title"]]})
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv, [NODE, RUN_JS, "sheets", "spreadsheets", "values", "get", "--params", PARAMS,
                                "--json", values])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(json.loads(argv[-1]), json.loads(values))

    def test_tracker_read_sends_valid_json_params(self):
        runner = Recorder(stdout='{"values": [["Job Title", "Company"]]}')
        tracker = st.SheetTracker(st.GwsClient([NODE, RUN_JS], runner=runner), "sheet-123", "Sheet1")
        self.assertEqual(tracker.rows(), [["Job Title", "Company"]])
        argv = runner.calls[0][0]
        self.assertEqual(argv[2:6], ["sheets", "spreadsheets", "values", "get"])
        self.assertEqual(json.loads(argv[argv.index("--params") + 1]),
                         {"spreadsheetId": "sheet-123", "range": "Sheet1!A:S"})

    def test_cmd_shim_refuses_values_cmd_exe_would_reinterpret(self):
        runner = Recorder()
        client = st.GwsClient([NPM + r"\gws.cmd"], runner=runner)
        for unsafe in ('{"values": [["AT&T"]]}', '{"v": "a|b"}', '{"v": "50%"}', '{"v": "x^y"}', '{"v": "<b>"}'):
            with self.subTest(unsafe), self.assertRaises(st.SheetsError) as err:
                client("sheets", "+append", "--json", unsafe)
            self.assertEqual(err.exception.code, st.SYNC_FAILED)
        self.assertEqual(runner.calls, [])  # nothing was started
        client("sheets", "spreadsheets", "values", "get", "--params", PARAMS)  # "!" in ranges is fine
        self.assertEqual(len(runner.calls), 1)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_json_survives_a_real_windows_process_boundary(self):
        """The list form reaches a real node process byte for byte (what PowerShell 5.1 would mangle)."""
        args = [PARAMS, json.dumps({"values": [['He said "hi"', "AT&T", "C:\\path\\", "a b", ""]]}), '{"k":"v"}']
        echo = "process.stdout.write(JSON.stringify(process.argv.slice(1)))"
        result = subprocess.run([shutil.which("node"), "-e", echo, *args], capture_output=True, text=True,
                                encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), args)


class TestErrorHandling(unittest.TestCase):
    def call(self, **kwargs):
        return st.GwsClient([NODE, RUN_JS], runner=Recorder(**kwargs))("sheets", "spreadsheets", "get")

    def test_launch_failures_are_gws_not_installed(self):
        for error in (FileNotFoundError(2, "not found"), OSError(193, "%1 is not a valid Win32 application")):
            with self.subTest(type(error).__name__), self.assertRaises(st.SheetsError) as err:
                self.call(raises=error)
            self.assertEqual(err.exception.code, st.GWS_NOT_INSTALLED)

    def test_invalid_params_json_is_reported_with_its_cause(self):
        stdout = '{"error": {"message": "Invalid --params JSON: key must be a string at line 1 column 2"}}'
        stderr = "Using keyring backend: file\nerror[validation]: Invalid --params JSON: key must be a string\n"
        with self.assertRaises(st.SheetsError) as err:
            self.call(returncode=1, stdout=stdout, stderr=stderr)
        self.assertEqual(err.exception.code, st.SYNC_FAILED)
        self.assertIn("Invalid --params JSON", str(err.exception))
        self.assertIn("run.js", str(err.exception))

    def test_auth_and_timeout_classification_unchanged(self):
        with self.assertRaises(st.SheetsError) as auth:
            self.call(returncode=1, stderr="Error: invalid_grant: Token has been expired or revoked")
        self.assertEqual(auth.exception.code, st.AUTH_REQUIRED)
        with self.assertRaises(st.SheetsError) as slow:
            self.call(raises=subprocess.TimeoutExpired("gws", 60))
        self.assertEqual(slow.exception.code, st.SYNC_FAILED)
        with self.assertRaises(st.SheetsError) as garbled:
            self.call(stdout="not json")
        self.assertEqual(garbled.exception.code, st.SYNC_FAILED)


if __name__ == "__main__":
    unittest.main()
