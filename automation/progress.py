"""Shared, flushed CLI progress with private logs and honest idle heartbeats."""

import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

from common import JAKARTA

HEARTBEAT_SECONDS = 10
_CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_EMAIL = re.compile(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_QUERY_SECRET = re.compile(r'([?&](?:code|state|code_challenge|code_verifier|access_token|refresh_token|id_token|client_secret|client_id)=)[^\s&#]+', re.I)
_FIELD_SECRET = re.compile(r'''(["']?(?:access_token|refresh_token|id_token|client_secret|code_verifier|authorization)["']?\s*[:=]\s*)(?:["'][^"']*["']|[^\s,;}]+)''', re.I)
_TOKEN = re.compile(r'\b(?:ya29\.[\w.-]+|1//[\w.-]+|GOCSPX-[\w-]+)')


def terminal_text(value):
    """Flatten control characters so external headlines cannot inject terminal escapes."""
    return _CONTROL.sub(' ', str(value)).strip()


class Progress:
    """Write one-line status records without colors, spinners, or buffered output."""

    def __init__(self, name, heartbeat=HEARTBEAT_SECONDS, report=None):
        """Start console reporting before configuration checks or any blocking work."""
        self.name = name
        self.started = time.monotonic()
        self.last_output = self.started
        self.activity_started = self.started
        self.activity = 'Starting'
        self.heartbeat = max(0.01, float(heartbeat))
        self.report = report
        self.path = None
        self.stream = None
        self.output = sys.stdout
        self.guard = threading.RLock()
        self.stop = threading.Event()
        self.closed = False
        self.secrets = set()
        self.pending = []
        self.tasks = {}
        self.log_error = None
        self.thread = threading.Thread(target=self._heartbeat, name=f'{name}-progress', daemon=True)
        self.thread.start()

    def protect(self, *values):
        """Redact known sensitive values and their URL-encoded forms on both outputs."""
        with self.guard:
            for value in values:
                if isinstance(value, str) and value:
                    self.secrets.update((value, quote(value, safe=''), quote_plus(value)))

    def clean(self, value):
        """Remove tokens, email identifiers, and terminal control sequences from logs."""
        text = str(value)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, '[redacted]')
        text = _QUERY_SECRET.sub(r'\1[redacted]', text)
        text = _FIELD_SECRET.sub(r'\1[redacted]', text)
        text = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [redacted]', text)
        text = _TOKEN.sub('[redacted]', text)
        text = _EMAIL.sub('[email]', text)
        return terminal_text(text)

    def _console(self, line):
        """Flush using an encoding-safe representation even on legacy Windows consoles."""
        encoding = getattr(self.output, 'encoding', None) or 'utf-8'
        safe = line.encode(encoding, errors='backslashreplace').decode(encoding)
        try:
            print(safe, file=self.output, flush=True)
        except (BrokenPipeError, OSError, ValueError):
            # Losing an output pipe must not turn a completed remote write into a retry.
            self.output = None

    def _write(self, message, level='INFO', active=True):
        """Serialize heartbeat and main-thread output, updating the optional report."""
        with self.guard:
            if self.closed:
                return
            now = time.monotonic()
            safe = self.clean(message)
            elapsed = now - self.started
            if active:
                self.activity = safe
                self.activity_started = now
                task = self.tasks.get(threading.get_ident())
                if task is not None:
                    task.update(activity=safe, activity_started=now)
            self.last_output = now
            stamp = datetime.now(JAKARTA).isoformat(timespec='seconds')
            line = f'[{stamp} +{elapsed:.1f}s] [{self.name}] {level:<5} {safe}'
            if self.output is not None:
                self._console(line)
            if self.stream is not None:
                try:
                    self.stream.write(line + '\n')
                    self.stream.flush()
                except OSError as error:
                    self.log_error = type(error).__name__
                    try:
                        self.stream.close()
                    except OSError:
                        pass
                    self.stream = None
                    if self.output is not None:
                        self._console(f'[{self.name}] WARN  Progress log unavailable ({self.log_error}); console reporting continues.')
            elif self.path is None:
                self.pending.append(line)
                self.pending = self.pending[-100:]
            if self.report is not None:
                self.report['elapsedSeconds'] = round(elapsed, 1)
                if active:
                    self.report['lastProgress'] = safe

    def __call__(self, message):
        """Support existing hunter callbacks with the common status format."""
        self.info(message)

    def info(self, message):
        """Report a started or ongoing operation."""
        self._write(message)

    def success(self, message):
        """Report a completed step without implying untested work succeeded."""
        self._write(message, 'OK')

    def warning(self, message):
        """Report a recoverable problem or explicit limitation."""
        self._write(message, 'WARN')

    def error(self, message):
        """Report a failed operation with sensitive values removed."""
        self._write(message, 'ERROR')

    def attach(self, path):
        """Keep the current and previous log, never touching credentials or sync state."""
        path = Path(path)
        previous = path.with_name(path.stem + '.previous' + path.suffix)
        with self.guard:
            if self.path is not None:
                raise ValueError('A progress log is already attached.')
            for candidate in (path, previous):
                if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
                    raise ValueError('Progress logs cannot be links or junctions.')
                if candidate.exists() and not candidate.is_file():
                    raise ValueError('A progress-log destination is not a regular file.')
            if path.exists():
                os.replace(path, previous)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self.path = path
            self.stream = os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n')
            for line in self.pending:
                self.stream.write(line + '\n')
            self.pending.clear()
            self.stream.flush()
        self.info('Progress log: ' + str(path))

    @contextmanager
    def task(self, name):
        """Track each collector's last operation independently for a concurrent heartbeat."""
        identity = threading.get_ident()
        with self.guard:
            prior = self.tasks.get(identity)
            self.tasks[identity] = {'name': self.clean(name), 'activity': 'Starting',
                                    'activity_started': time.monotonic()}
        try:
            yield
        finally:
            with self.guard:
                if prior is None:
                    self.tasks.pop(identity, None)
                else:
                    self.tasks[identity] = prior

    @contextmanager
    def waiting(self, description, announce=False):
        """Expose a nested blocking operation to the idle heartbeat, without extra I/O."""
        with self.guard:
            prior = (self.activity, self.activity_started)
            self.activity = self.clean(description)
            self.activity_started = time.monotonic()
        if announce:
            self.info(description)
        try:
            yield
        finally:
            with self.guard:
                self.activity, self.activity_started = prior

    def console_data(self, value, description):
        """Show explicitly requested calendar details only in the local console, not logs."""
        self.info(description + ' (shown below; values are not copied into the progress log).')
        with self.guard:
            if self.output is not None:
                # The caller provides a JSON-encoded string, never OAuth credentials.
                self._console(terminal_text(value))

    def _heartbeat(self):
        """Report silence honestly; a heartbeat is not evidence of remote progress."""
        while not self.stop.wait(min(0.5, self.heartbeat / 2)):
            with self.guard:
                now = time.monotonic()
                if self.closed:
                    return
                if now - self.last_output >= self.heartbeat:
                    if self.tasks:
                        pending = '; '.join(f'{task["name"]}: {task["activity"]} ({now - task["activity_started"]:.0f}s in operation)'
                                            for task in sorted(self.tasks.values(), key=lambda row: row['name']))
                        self._write('Still waiting on active collectors: ' + pending + '; no completion yet.', 'WAIT', active=False)
                        continue
                    duration = now - self.activity_started
                    self._write(f'Still waiting: {self.activity} | {duration:.0f}s in this operation; no completion yet.', 'WAIT', active=False)

    def close(self):
        """Stop the heartbeat and close the log on success, failure, or Ctrl+C."""
        self.stop.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        with self.guard:
            if self.closed:
                return
            self.closed = True
            if self.stream is not None:
                try:
                    self.stream.flush()
                    self.stream.close()
                except OSError:
                    pass
                self.stream = None


@contextmanager
def wait_for(progress, description):
    """Keep library calls silent when no CLI reporter was supplied."""
    if progress is None:
        yield
    else:
        with progress.waiting(description):
            yield
