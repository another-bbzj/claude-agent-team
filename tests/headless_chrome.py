"""Start one headless Chrome and read the DevTools port it actually bound.

CI used to reserve a port, close it, then pass that port to Chrome. Something
else can bind the same port before Chrome does, so `/json/version` stays
refused for the whole wait. stderr was discarded, which left only
"chrome debug port not ready".

Chrome is started with ``--remote-debugging-port=0`` and a fresh profile.
The real port is read from ``<user-data-dir>/DevToolsActivePort`` or from a
``DevTools listening on ws://...`` line on stderr. Startup is retried once.
A failure includes the stderr tail.
"""
import http.client
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

CHROME_NAMES = (
    'google-chrome',
    'google-chrome-stable',
    'chromium',
    'chromium-browser',
)
STARTUP_TIMEOUT = 15.0
STARTUP_ATTEMPTS = 2
POLL_SECONDS = 0.05
STDERR_TAIL_CHARS = 8192
ACTIVE_PORT_NAME = 'DevToolsActivePort'

# The path after the port is required so a half-written "364" is not a port.
_LISTENING_RE = re.compile(
    r'DevTools listening on ws://(?:\[[^\]\s]+\]|[^/\s:]+):(\d+)/devtools/'
)


class ChromeLaunchError(RuntimeError):
    """Headless Chrome did not expose a DevTools port."""


class HeadlessChrome(object):
    """One running headless Chrome whose DevTools port is already known."""

    def __init__(self, port, process, root, terminate):
        self.port = port
        self.process = process
        self.root = root
        self._terminate = terminate
        self._stopped = False

    def stderr_tail(self, limit=STDERR_TAIL_CHARS):
        return _tail(os.path.join(self.root, 'stderr.txt'), limit)

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._terminate(self.process)
        remove_chrome_profile(self.root)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


def chrome_binary():
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return ''


def parse_devtools_port(text):
    """Return a DevTools TCP port from one text buffer, or None.

    A complete ``DevTools listening on ws://host:port/devtools/...`` line wins.
    Otherwise the buffer must look like ``DevToolsActivePort``: a port line
    followed by a ``/devtools/...`` path. A one-line or truncated write is
    ignored so a partial port is not treated as ready.
    """
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    elif not isinstance(text, str):
        return None
    match = _LISTENING_RE.search(text)
    if match:
        return _valid_port(match.group(1))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[1].startswith('/devtools/'):
        return None
    return _valid_port(lines[0])


def read_devtools_port(profile, stderr_path=''):
    """Prefer this profile's DevToolsActivePort file, then stderr."""
    port = parse_devtools_port(_read_text(os.path.join(profile, ACTIVE_PORT_NAME)))
    if port:
        return port
    if stderr_path:
        return parse_devtools_port(_tail(stderr_path, limit=65536))
    return None


def devtools_ready(port, timeout=0.5):
    """Return True when ``/json/version`` exposes a browser websocket."""
    conn = None
    try:
        conn = http.client.HTTPConnection('127.0.0.1', int(port), timeout=timeout)
        conn.request('GET', '/json/version')
        response = conn.getresponse()
        body = response.read()
        return response.status == 200 and b'webSocketDebuggerUrl' in body
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def remove_chrome_profile(directory, timeout=5):
    """Delete a Chrome profile without failing the test.

    Python 3.8's TemporaryDirectory has no ignore_cleanup_errors. Chrome can
    recreate Default/ while rmtree is removing it, which raises ENOTEMPTY.
    """
    path = str(directory)
    deadline = time.time() + timeout
    while os.path.exists(path):
        _kill_processes_using(path)
        try:
            shutil.rmtree(path)
        except OSError:
            if time.time() >= deadline:
                shutil.rmtree(path, ignore_errors=True)
                return
            time.sleep(0.05)
            continue
        if not os.path.exists(path):
            return


def launch_headless_chrome(
        binary,
        timeout=STARTUP_TIMEOUT,
        attempts=STARTUP_ATTEMPTS,
        popen=None,
        sleep=None,
        clock=None,
        ready=None,
        mkdtemp=None,
        terminate=None):
    """Start headless Chrome and return it once DevTools answers.

    Each attempt gets a new profile and ``--remote-debugging-port=0``.
    The default is one retry. The raised error includes each attempt's
    stderr tail.
    """
    if not binary:
        raise ChromeLaunchError('chrome binary is missing')
    if attempts < 1:
        raise ValueError('attempts must be positive')
    popen = popen or subprocess.Popen
    sleep = sleep or time.sleep
    clock = clock or time.time
    ready = ready or devtools_ready
    mkdtemp = mkdtemp or tempfile.mkdtemp
    terminate = terminate or _terminate_process
    notes = []
    for attempt in range(1, attempts + 1):
        result = _start_attempt(
            binary, attempt, timeout, popen, sleep, clock, ready, mkdtemp, terminate,
        )
        if isinstance(result, HeadlessChrome):
            return result
        notes.append(result)
    raise ChromeLaunchError(
        'headless Chrome did not open a DevTools port after %d attempt(s)\n%s' % (
            attempts, '\n\n'.join(notes),
        )
    )


def _valid_port(value):
    if value is None or not str(value).isdigit():
        return None
    port = int(value)
    if 1 <= port <= 65535:
        return port
    return None


def _read_text(path, limit=4096):
    try:
        with open(path, 'rb') as handle:
            data = handle.read(limit)
    except OSError:
        return ''
    return data.decode('utf-8', 'replace')


def _tail(path, limit=STDERR_TAIL_CHARS):
    size = 0
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit), os.SEEK_SET)
            data = handle.read(limit)
    except OSError:
        return ''
    text = data.decode('utf-8', 'replace')
    if size > limit:
        newline = text.find('\n')
        if newline >= 0:
            text = text[newline + 1:]
    return text


def _chrome_command(binary, profile):
    return [
        binary,
        '--headless=new',
        '--no-sandbox',
        '--disable-gpu',
        '--disable-dev-shm-usage',
        '--disable-extensions',
        '--no-first-run',
        '--no-default-browser-check',
        '--window-size=1280,900',
        '--remote-debugging-port=0',
        '--user-data-dir=%s' % profile,
        'about:blank',
    ]


def _spawn_kwargs():
    kwargs = {
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.DEVNULL,
    }
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    return kwargs


def _terminate_process(process):
    if process is None:
        return
    pid = getattr(process, 'pid', None)
    if pid:
        try:
            if os.name == 'nt':
                subprocess.call(
                    ['taskkill', '/F', '/T', '/PID', str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif hasattr(os, 'killpg') and hasattr(signal, 'SIGKILL'):
                os.killpg(pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _kill_processes_using(directory):
    """Stop Chrome children still writing a user-data directory. /proc is Linux-only."""
    if not hasattr(signal, 'SIGKILL'):
        return
    proc = Path('/proc')
    if not proc.is_dir():
        return
    needle = os.fsencode(str(directory))
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / 'cmdline').read_bytes()
        except OSError:
            continue
        if needle not in command:
            continue
        try:
            os.kill(int(entry.name), signal.SIGKILL)
        except OSError:
            pass


def _failure_note(attempt, exc, stderr_path):
    tail = _tail(stderr_path).strip()
    note = 'attempt %d: %s' % (attempt, exc)
    if tail:
        note = '%s\n%s' % (note, tail)
    return note


def _discard(root, process, terminate):
    if process is not None:
        terminate(process)
    remove_chrome_profile(root)


def _wait_for_port(profile, stderr_path, process, timeout, clock, sleep, ready):
    deadline = clock() + timeout
    while True:
        port = read_devtools_port(profile, stderr_path)
        if port and ready(port):
            return port
        status = process.poll()
        if status is not None:
            raise ChromeLaunchError(
                'chrome exited with status %s before opening a DevTools port' % status
            )
        if clock() >= deadline:
            raise ChromeLaunchError(
                'timed out after %ss waiting for a DevTools port' % timeout
            )
        sleep(POLL_SECONDS)


def _start_attempt(binary, attempt, timeout, popen, sleep, clock, ready, mkdtemp, terminate):
    root = mkdtemp(prefix='headless-chrome-')
    profile = os.path.join(root, 'profile')
    stderr_path = os.path.join(root, 'stderr.txt')
    process = None
    try:
        os.makedirs(profile)
        with open(stderr_path, 'wb') as stderr_handle:
            try:
                process = popen(
                    _chrome_command(binary, profile),
                    stderr=stderr_handle,
                    **_spawn_kwargs(),
                )
            except OSError as exc:
                raise ChromeLaunchError('failed to spawn: %s' % exc)
        port = _wait_for_port(profile, stderr_path, process, timeout, clock, sleep, ready)
    except ChromeLaunchError as exc:
        note = _failure_note(attempt, exc, stderr_path)
        _discard(root, process, terminate)
        return note
    except Exception:
        _discard(root, process, terminate)
        raise
    return HeadlessChrome(port, process, root, terminate)
