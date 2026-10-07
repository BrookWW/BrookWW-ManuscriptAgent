"""Run real Seatbelt tests with AUDITAGENT_SANDBOX_TESTS=1 in a normal terminal."""

import ctypes
import json
import mmap
import os
from pathlib import Path
import platform
import plistlib
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from isolation import IsolationError, Seatbelt


class PolicyTests(unittest.TestCase):
    @patch("isolation.platform.system", return_value="Linux")
    def test_unsupported_system_fails_closed(self, _mock):
        with self.assertRaises(IsolationError):
            Seatbelt([], [])

    @unittest.skipUnless(platform.system() == "Darwin", "macOS policy syntax")
    def test_policy_has_no_unrestricted_filesystem_or_network(self):
        policy = Seatbelt([Path("/private/tmp/read")], [Path("/private/tmp/write")],
                          [Path("/private/tmp/archive")], proxy_port=12345)
        self.assertIn('(remote tcp "localhost:12345")', policy.profile)
        self.assertIn("deny file-link", policy.profile)
        self.assertIn("deny mach-lookup", policy.profile)
        self.assertIn("file-map-executable", policy.profile)
        self.assertEqual(policy.wrap(["/bin/echo", "ok"])[-2:], ["/bin/echo", "ok"])
        with self.assertRaises(IsolationError):
            Seatbelt([Path("/")], [])

    @unittest.skipUnless(platform.system() == "Darwin", "macOS policy syntax")
    def test_macos_preferences_sync_is_an_explicit_narrow_opt_in(self):
        ordinary = Seatbelt([], [])
        self.assertIn("(deny mach-lookup mach-priv*", ordinary.profile)
        self.assertIn("(deny ipc-posix*", ordinary.profile)
        compatible = Seatbelt([], [], allow_macos_preferences_sync=True)
        self.assertIn('(global-name "com.apple.cfprefsd.agent")', compatible.profile)
        self.assertIn('(global-name "com.apple.cfprefsd.daemon")', compatible.profile)
        self.assertIn(f'(ipc-posix-name "apple.cfprefs.{os.getuid()}v1")', compatible.profile)
        self.assertIn('(ipc-posix-name "apple.cfprefs.daemonv1")', compatible.profile)
        self.assertIn("(deny ipc-sysv* iokit* user-preference*)", compatible.profile)
        self.assertIn("(deny ipc-posix-sem* ipc-posix-shm-write*)", compatible.profile)
        self.assertNotIn("ipc-posix-name-prefix", compatible.profile)
        self.assertNotIn("preference-domain", compatible.profile)
        self.assertNotIn("opendirectoryd", compatible.profile)
        with self.assertRaises(ValueError):
            Seatbelt([], [], allow_macos_preferences_sync="yes")


@unittest.skipUnless(platform.system() == "Darwin" and os.environ.get("AUDITAGENT_SANDBOX_TESTS") == "1",
                     "requires explicit real macOS sandbox tests from an unsandboxed terminal")
class KernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="auditagent-isolation-", dir="/private/tmp")
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.archive = self.root / "archive"
        self.work.mkdir()
        self.archive.mkdir()
        self.canary = self.archive / "synthetic-canary"
        self.canary.write_text("Synthetic fixture only; never user data.")
        self.sealed = self.work / "sealed"
        self.sealed.write_text("fixed")
        self.policy = Seatbelt([self.work, Path(sys.prefix).resolve()], [self.work],
                               [self.archive], [self.sealed])

    def tearDown(self):
        self.temp.cleanup()

    def run_python(self, code, policy=None):
        return subprocess.run((policy or self.policy).wrap([sys.executable, "-I", "-c", code]),
                              cwd=self.work, capture_output=True, text=True, close_fds=True)

    def test_direct_descendant_symlink_link_rename_and_write_denied(self):
        self.assertTrue(all(self.policy.preflight(self.work, self.canary).values()))

    def test_zsh_parent_and_descendant_heredocs_use_session_temp_without_archive_access(self):
        from manuscript import private_environment
        temporary = self.work / "tmp"
        temporary.mkdir()
        env = private_environment(self.work, temporary)
        command = f'''{shlex.quote(sys.executable)} -I - <<'PY'
import pathlib
try:
    pathlib.Path({str(self.canary)!r}).read_bytes()
except PermissionError:
    print('archive-denied')
else:
    raise AssertionError('archive read was allowed')
print('heredoc-ok')
PY
'''
        # Use the same policy, not a new global /tmp write exception. The
        # nested zsh checks that the prefix is exported to descendant tools.
        for code in (command, "/bin/zsh -f -c " + shlex.quote(command)):
            result = subprocess.run(self.policy.wrap(["/bin/zsh", "-f", "-c", code]),
                                    cwd=self.work, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10, close_fds=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "archive-denied\nheredoc-ok\n")
        self.assertTrue(all(self.policy.preflight(self.work, self.canary, env=env).values()))

    def test_exact_ancestor_grants_allow_no_follow_walk_but_not_sibling_contents(self):
        allowed = self.work / "paper.tex"
        allowed.write_text("current")
        unrelated = self.root / "unrelated.txt"
        unrelated.write_text("unrelated synthetic data")
        code = f'''import os,pathlib
path=pathlib.Path({str(allowed)!r})
fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
for name in path.parts[1:-1]:
 child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
 os.close(fd);fd=child
file=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
assert os.read(file,100)==b'current'
os.close(file);os.close(fd)
try:
 pathlib.Path({str(self.archive)!r}).iterdir().__next__()
except PermissionError:
 pass
else:
 raise AssertionError('archive listing was allowed')
try:
 pathlib.Path({str(unrelated)!r}).read_bytes()
except PermissionError:
 pass
else:
 raise AssertionError('sibling file read was allowed')
'''
        result = self.run_python(code)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sealed_control_and_archive_directory_cannot_change(self):
        for code in (f"open({str(self.sealed)!r},'w')", f"__import__('os').rename({str(self.archive)!r}, {str(self.root/'moved')!r})"):
            result = self.run_python(code)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PermissionError", result.stderr)

    def test_sealed_file_ancestors_cannot_be_renamed_but_siblings_are_writable(self):
        home = self.work / "home"
        config_dir = home / ".codex"
        config_dir.mkdir(parents=True)
        config = config_dir / "config.toml"
        config.write_text("sealed=true\n")
        policy = Seatbelt([self.work, Path(sys.prefix).resolve()], [self.work],
                          [self.archive], [config])
        for directory in (config_dir, home):
            code = f"__import__('os').rename({str(directory)!r}, {str(directory.with_name(directory.name + '-moved'))!r})"
            result = self.run_python(code, policy)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PermissionError", result.stderr)
        result = self.run_python(
            f"__import__('pathlib').Path({str(config_dir / 'session.json')!r}).write_text('current round')", policy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config.read_text(), "sealed=true\n")

    def test_denied_subtree_cannot_be_exposed_by_renaming_its_parent(self):
        container = self.work / "container"
        protected = container / "protected"
        protected.mkdir(parents=True)
        (protected / "canary").write_text("synthetic history")
        policy = Seatbelt([self.work, Path(sys.prefix).resolve()], [self.work],
                          [protected])
        result = self.run_python(
            f"__import__('os').rename({str(container)!r}, {str(self.work / 'moved')!r})", policy)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PermissionError", result.stderr)

    def test_hardlinks_cannot_be_created_even_within_workspace(self):
        result = self.run_python(f"__import__('os').link({str(self.sealed)!r}, {str(self.work/'alias')!r})")
        self.assertNotEqual(result.returncode, 0)

    def test_preexisting_hardlink_is_not_a_security_boundary(self):
        # The runner must reject aliases / copy bytes into new inodes.
        alias = self.work / "preexisting-alias"
        os.link(self.canary, alias)
        result = self.run_python(f"open({str(alias)!r}).read()")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(alias.stat().st_nlink, 1)

    def test_network_requires_the_specific_proxy_port(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            code = f"import socket; socket.create_connection(('127.0.0.1',{port}),timeout=2).close()"
            self.assertNotEqual(self.run_python(code).returncode, 0)
            relay_policy = Seatbelt([self.work, Path(sys.prefix).resolve()], [self.work],
                                   [self.archive], proxy_port=port)
            result = self.run_python(code, relay_policy)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_source_port_does_not_open_other_local_services_or_archives(self):
        with socket.socket() as relay, socket.socket() as sources, socket.socket() as unrelated:
            for server in (relay, sources, unrelated):
                server.bind(("127.0.0.1", 0))
                server.listen()
            ports = [server.getsockname()[1] for server in (relay, sources, unrelated)]
            policy = Seatbelt([self.work, Path(sys.prefix).resolve()], [self.work], [self.archive],
                              proxy_port=ports[0], source_port=ports[1])
            for port in ports[:2]:
                result = self.run_python(f"import socket; socket.create_connection(('127.0.0.1',{port}),timeout=2).close()", policy)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = self.run_python(f"import socket; socket.create_connection(('127.0.0.1',{ports[2]}),timeout=2).close()", policy)
            self.assertNotEqual(result.returncode, 0)
            result = self.run_python(f"open({str(self.canary)!r}).read()", policy)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PermissionError", result.stderr)

    def test_macos_preferences_sync_keeps_archive_and_container_reads_isolated(self):
        policy = Seatbelt([self.work, Path(sys.prefix).resolve()], [self.work],
                          [self.archive], [self.sealed], allow_macos_preferences_sync=True)
        self.assertTrue(all(policy.preflight(self.work, self.canary).values()))
        containers = [self.work / "container", self.archive / "container",
                      self.root / "prior-session"]
        for container in containers:
            for relative in ("Library/Preferences", "Data/Library/Preferences"):
                directory = container / relative
                directory.mkdir(parents=True)
                (directory / "com.openai.codex.plist").write_bytes(
                    plistlib.dumps({"synthetic_key": "synthetic sentinel"}))
        code = _CF_CONTAINER_READ_PROBE + f"\nprobe({str(containers[0])!r}, {str(containers[1])!r}, {str(containers[2])!r})\n"
        # Verify the SPI signature and fixture layout independently: both
        # containers are readable before applying the sandbox.
        control = subprocess.run([sys.executable, "-I", "-c", code],
                                 cwd=self.work, capture_output=True, text=True,
                                 timeout=10, close_fds=True)
        self.assertEqual(control.returncode, 0, control.stderr)
        outside = json.loads(control.stdout)
        self.assertTrue(all(outside["workspace"]))
        self.assertTrue(all(outside["archive"]))
        self.assertTrue(all(outside["previous"]))
        confined = self.run_python(code, policy)
        self.assertEqual(confined.returncode, 0, confined.stderr)
        inside = json.loads(confined.stdout)
        self.assertTrue(inside["synchronize"])
        self.assertTrue(all(inside["workspace"]))
        self.assertFalse(any(inside["archive"]))
        self.assertFalse(any(inside["previous"]))
        listing = self.run_python(f"list(__import__('pathlib').Path({str(self.archive)!r}).iterdir())", policy)
        self.assertNotEqual(listing.returncode, 0)
        self.assertIn("PermissionError", listing.stderr)

    def test_macos_preferences_sync_rejects_cf_container_archive_writes(self):
        policy = Seatbelt([self.work, Path(sys.prefix).resolve()], [self.work],
                          [self.archive], allow_macos_preferences_sync=True)
        containers = [self.work / "write-container", self.archive / "write-container"]
        files = []
        original = plistlib.dumps({"synthetic_key": "original sentinel"})
        for container in containers:
            directory = container / "Library/Preferences"
            directory.mkdir(parents=True)
            target = directory / "com.openai.codex.plist"
            target.write_bytes(original)
            files.append(target)
        outside_code = _CF_CONTAINER_READ_PROBE + (
            f"\nwrite_probe({str(containers[0])!r}, {str(containers[1])!r}, 'outside replacement')\n")
        control = subprocess.run([sys.executable, "-I", "-c", outside_code],
                                 cwd=self.work, capture_output=True, text=True,
                                 timeout=10, close_fds=True)
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertTrue(all(json.loads(control.stdout).values()))
        for target in files:
            self.assertEqual(plistlib.loads(target.read_bytes())["synthetic_key"], "outside replacement")
            target.write_bytes(original)
        confined_code = _CF_CONTAINER_READ_PROBE + (
            f"\nwrite_probe({str(containers[0])!r}, {str(containers[1])!r}, 'confined replacement')\n")
        confined = self.run_python(confined_code, policy)
        self.assertEqual(confined.returncode, 0, confined.stderr)
        result = json.loads(confined.stdout)
        self.assertFalse(result["archive"])
        # A sync acknowledgment alone is not proof of an actual preference
        # write. The unsandboxed control above demonstrates real writes, while
        # the archive must reject synchronization and keep identical bytes.
        self.assertEqual(files[1].read_bytes(), original)

    def test_macos_preferences_sync_keeps_unrelated_ipc_and_all_shm_writes_denied(self):
        libc = ctypes.CDLL(None, use_errno=True)
        # Darwin shm_open is variadic; ARM64 passes its mode on the stack.
        libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
        libc.shm_open.restype = ctypes.c_int
        libc.shm_unlink.argtypes = [ctypes.c_char_p]
        libc.shm_unlink.restype = ctypes.c_int
        stem = ("ag-pref-" + os.urandom(6).hex()).encode()
        fd = libc.shm_open(stem, os.O_CREAT | os.O_EXCL | os.O_RDWR, ctypes.c_int(0o600))
        self.assertGreaterEqual(fd, 0, f"Could not create synthetic shm: errno {ctypes.get_errno()}")
        try:
            os.ftruncate(fd, 4)
            with mmap.mmap(fd, 4, access=mmap.ACCESS_WRITE) as shared:
                shared[:] = b"SAFE"
            os.close(fd)
            fd = -1
            policy = Seatbelt([self.work, Path(sys.prefix).resolve()], [self.work],
                              [self.archive], allow_macos_preferences_sync=True)
            code = _IPC_SCOPE_PROBE + f"\nprobe({stem!r}, {stem + b'-new'!r})\n"
            result = self.run_python(code, policy)
            self.assertEqual(result.returncode, 0, result.stderr)
            checks = json.loads(result.stdout)
            self.assertTrue(all(checks.values()), checks)
            # Child unlink/write attempts must leave the parent-owned object
            # intact; a before/after positive control proves real isolation.
            fd = libc.shm_open(stem, os.O_RDONLY, ctypes.c_int(0))
            self.assertGreaterEqual(fd, 0, f"Synthetic shm no longer readable: errno {ctypes.get_errno()}")
            with mmap.mmap(fd, 4, access=mmap.ACCESS_READ) as shared:
                self.assertEqual(shared[:], b"SAFE")
        finally:
            if fd >= 0:
                os.close(fd)
            libc.shm_unlink(stem)
            libc.shm_unlink(stem + b"-new")

    def test_macos_preferences_sync_native_cli_config_without_auth_or_model_turn(self):
        selected = os.environ.get("MANUSCRIPT_CODEX_TEST_BINARY") or shutil.which("codex")
        if not selected:
            self.skipTest("Codex CLI is unavailable")
        executable = Path(selected).resolve()
        if not executable.is_file():
            self.skipTest("Codex CLI executable is unavailable")
        package = executable.parent.parent
        manifest = package / "codex-package.json"
        if not manifest.is_file():
            self.skipTest("Requires an installed standalone Codex package")
        home, temporary = self.work / "home", self.work / "tmp"
        (home / ".codex").mkdir(parents=True)
        temporary.mkdir()
        config = home / ".codex/config.toml"
        config.write_text('''cli_auth_credentials_store = "file"
check_for_update_on_startup = false
[features]
apps = false
plugins = false
hooks = false
memories = false
''')
        from manuscript import private_environment
        env = private_environment(home, temporary)
        self.assertFalse((home / ".codex/auth.json").exists())
        policy = Seatbelt([self.work, package, Path(sys.prefix).resolve()], [self.work],
                          [self.archive], [config], allow_macos_preferences_sync=True)
        command = policy.wrap([str(executable), "app-server", "--listen", "stdio://",
                               "-c", 'model="gpt-6.1-sol"',
                               "-c", 'model_reasoning_effort="ultra"'])
        proc = subprocess.Popen(command, cwd=self.work, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        buffer = bytearray()

        def send(request):
            proc.stdin.write((json.dumps(request) + "\n").encode())
            proc.stdin.flush()

        def response(request_id):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                for _key, _events in selector.select(0.25):
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    self.assertTrue(chunk, "Native configuration server closed unexpectedly")
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        line, _, remaining = buffer.partition(b"\n")
                        buffer[:] = remaining
                        item = json.loads(line)
                        if item.get("id") == request_id:
                            return item
            self.fail("Native configuration RPC timed out")

        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "isolation_test", "version": "1"},
                "capabilities": {"experimentalApi": True}}})
            self.assertNotIn("error", response(1))
            send({"method": "initialized"})
            send({"id": 2, "method": "config/read", "params": {"includeLayers": False}})
            reply = response(2)
            self.assertNotIn("error", reply)
            resolved = reply["result"]["config"]
            self.assertEqual(resolved["model"], "gpt-6.1-sol")
            self.assertEqual(resolved["model_reasoning_effort"], "ultra")
            for feature in ("apps", "plugins", "hooks", "memories"):
                self.assertFalse(resolved["features"][feature])
            proc.stdin.close()
            proc.wait(timeout=5)
            errors = proc.stderr.read().decode(errors="replace")
            self.assertEqual(proc.returncode, 0, errors)
            self.assertNotIn("Failed to synchronize managed preferences", errors)
            self.assertFalse((home / ".codex/auth.json").exists())
            self.assertTrue(all(policy.preflight(self.work, self.canary, env=env).values()))
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            selector.close()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe and not pipe.closed:
                    pipe.close()


# Private SPI signatures are present in the macOS SDK export list and used in
# https://github.com/opa334/TrollStore/blob/main/RootHelper/main.m . Positive
# controls above verify the bindings and actual container layouts on this OS.
_CF_CONTAINER_READ_PROBE = r'''
import ctypes,json
cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
for name,args,rest in [
    ('CFStringCreateWithCString',[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32],ctypes.c_void_p),
    ('CFPreferencesAppSynchronize',[ctypes.c_void_p],ctypes.c_ubyte),
    ('CFPreferencesAppValueIsForced',[ctypes.c_void_p]*2,ctypes.c_ubyte),
    ('_CFPreferencesCopyValueWithContainer',[ctypes.c_void_p]*5,ctypes.c_void_p),
    ('_CFPreferencesCopyMultipleWithContainer',[ctypes.c_void_p]*5,ctypes.c_void_p),
    ('_CFPreferencesSetValueWithContainer',[ctypes.c_void_p]*6,None),
    ('_CFPreferencesSynchronizeWithContainer',[ctypes.c_void_p]*4,ctypes.c_ubyte),
    ('CFDictionaryGetValue',[ctypes.c_void_p]*2,ctypes.c_void_p),
    ('CFEqual',[ctypes.c_void_p]*2,ctypes.c_ubyte)]:
    function=getattr(cf,name);function.argtypes=args;function.restype=rest
def st(value):return cf.CFStringCreateWithCString(None,value.encode(),0x08000100)
def probe(workspace,archive,previous):
    app=st('com.openai.codex');key=st('synthetic_key');sentinel=st('synthetic sentinel')
    user=ctypes.c_void_p.in_dll(cf,'kCFPreferencesCurrentUser')
    host=ctypes.c_void_p.in_dll(cf,'kCFPreferencesAnyHost')
    for name in ['config_toml_base64','requirements_toml_base64']:
        cf.CFPreferencesAppValueIsForced(st(name),app)
    result={'synchronize':bool(cf.CFPreferencesAppSynchronize(app))}
    for kind,path in [('workspace',workspace),('archive',archive),('previous',previous)]:
        matches=[]
        for suffix in ['', '/Data']:
            container=st(path+suffix)
            value=cf._CFPreferencesCopyValueWithContainer(key,app,user,host,container)
            values=cf._CFPreferencesCopyMultipleWithContainer(None,app,user,host,container)
            multiple=cf.CFDictionaryGetValue(values,key) if values else None
            matches.extend([bool(value and cf.CFEqual(value,sentinel)),
                            bool(multiple and cf.CFEqual(multiple,sentinel))])
        result[kind]=matches
    print(json.dumps(result))
def write_probe(workspace,archive,replacement):
    app=st('com.openai.codex');key=st('synthetic_key');value=st(replacement)
    user=ctypes.c_void_p.in_dll(cf,'kCFPreferencesCurrentUser')
    host=ctypes.c_void_p.in_dll(cf,'kCFPreferencesAnyHost')
    result={}
    for kind,path in [('workspace',workspace),('archive',archive)]:
        container=st(path)
        cf._CFPreferencesSetValueWithContainer(key,value,app,user,host,container)
        result[kind]=bool(cf._CFPreferencesSynchronizeWithContainer(app,user,host,container))
    print(json.dumps(result))
'''


_IPC_SCOPE_PROBE = r'''
import ctypes,errno,json,os
libc=ctypes.CDLL(None,use_errno=True)
libc.shm_open.argtypes=[ctypes.c_char_p,ctypes.c_int]
libc.shm_open.restype=ctypes.c_int
libc.shm_unlink.argtypes=[ctypes.c_char_p];libc.shm_unlink.restype=ctypes.c_int
def probe(existing,new):
    result={}
    for name in [f'apple.cfprefs.{os.getuid()}v1'.encode(),b'apple.cfprefs.daemonv1']:
        fd=libc.shm_open(name,os.O_RDONLY,ctypes.c_int(0))
        result[name.decode()+'_read']=fd>=0
        if fd>=0:os.close(fd)
        ctypes.set_errno(0)
        fd=libc.shm_open(name,os.O_RDWR,ctypes.c_int(0))
        result[name.decode()+'_write_denied']=fd<0 and ctypes.get_errno() in (errno.EPERM,errno.EACCES)
        if fd>=0:os.close(fd)
    for label,name,flags in [('unknown_read',existing,os.O_RDONLY),
                             ('unknown_write',existing,os.O_RDWR),
                             ('create',new,os.O_CREAT|os.O_EXCL|os.O_RDWR)]:
        ctypes.set_errno(0);fd=libc.shm_open(name,flags,ctypes.c_int(0o600))
        result[label+'_denied']=fd<0 and ctypes.get_errno() in (errno.EPERM,errno.EACCES)
        if fd>=0:os.close(fd)
    ctypes.set_errno(0);status=libc.shm_unlink(existing)
    result['unlink_denied']=status<0 and ctypes.get_errno() in (errno.EPERM,errno.EACCES)
    print(json.dumps(result))
'''


if __name__ == "__main__":
    unittest.main()
