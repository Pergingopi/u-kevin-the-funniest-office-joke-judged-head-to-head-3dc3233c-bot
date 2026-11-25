#!/usr/bin/env python3
"""One office joke, written to a file.

The whole entry is one line of text in ``entry.txt`` under the directory
named by ``MAKEFUN_OUTPUT_DIR`` (``/output`` when that variable is empty
or missing). The line jokes about office life by treating the fire
drill as the perfect meeting: everyone turns up on time, everyone
leaves together, and nobody asks for the slides. Each of the three
beats is true of a drill and false of a genuine meeting, which is why
the punchline can sit at the end.

The program is intentionally dull: standard library only, no clock,
no randomness, no network, no third-party code. Anybody running it
anywhere gets byte-identical output, so the scorer always reads the
same valid entry.

Three other lines were tried against the brief and dropped:

* A printer line (110ch) that only complains -- no turn, so the ending
  feels flat and the reader guesses it from the first words.
* A meeting-about-a-meeting line (114ch) whose punchline repeats the
  setup; the circle is the joke, leaving nothing for the ending.
* A reply-all line (114ch) that leans on the most overused office joke
  around and ends on a muddled clause.

The kept line (116ch) wins on all three counts: one setup everyone
recognises, one turn, and the punchline last.

Writing is careful rather than clever. The bytes go to a temporary
file in the same directory, are flushed to disk, and only then take
the place of ``entry.txt`` by rename, so a crash can never leave a
half-written entry behind. The finished file is read back and checked
again -- size, bytes, encoding, the character gate, and its mode --
before the program reports success. Anything wrong exits 1 with a
single ``error: ...`` line and nothing on stdout.
"""

import os
import stat
import sys
import tempfile
import time
from pathlib import Path

ENTRY_TEXT = "The fire drill is the only meeting where everyone shows up on time, leaves together, and nobody asks for the slides."

MIN_LEN = 1
MAX_LEN = 200
OUT_NAME = "entry.txt"
OUT_MODE = 0o644
SCRAP_PREFIX = ".entry-"
SCRAP_TRIES = 3
SCRAP_PAUSE = 0.05
BAD_SEPARATORS = ("\u0085", "\u2028", "\u2029")


def _writable_dir(raw_value):
    """Turn the ``MAKEFUN_OUTPUT_DIR`` value into a directory to use.

    A blank value means ``/output``. A relative value is read against
    the working directory and made absolute, so output never lands
    somewhere the caller did not intend. The filesystem root is
    refused outright: sweeping or writing there is never wanted.
    A path blocked by a plain file, or a directory that cannot be
    written to, fails here with a plain message instead of halfway
    through the write.
    """
    wanted = (raw_value or "").strip() or "/output"
    if not os.path.isabs(wanted):
        wanted = os.path.abspath(wanted)
    wanted = os.path.normpath(wanted)
    if wanted == os.path.abspath(os.sep):
        raise ValueError("the filesystem root cannot be the output directory")
    if os.path.lexists(wanted) and not os.path.isdir(wanted):
        raise ValueError(f"a file blocks the output directory {wanted!r}")
    os.makedirs(wanted, exist_ok=True)
    if not os.path.isdir(wanted):
        raise ValueError(f"{wanted!r} is not a directory after creation")
    if not os.access(wanted, os.W_OK):
        raise ValueError(f"the output directory {wanted!r} cannot be written to")
    return Path(wanted)


def _check_text(text):
    """Check ``text`` against the gate and return it as UTF-8 bytes.

    The gate allows 1 to 200 characters. On top of that the line must
    be a single line: no control characters, no Unicode line
    separators, and no padding at either end, since padding would eat
    the budget and a reader might trim it. The first problem found
    raises ``ValueError`` with a specific message.
    """
    if not isinstance(text, str):
        raise ValueError("the entry is not a string")
    if not MIN_LEN <= len(text) <= MAX_LEN:
        raise ValueError(
            f"the entry is {len(text)} characters long, the gate allows "
            f"{MIN_LEN}-{MAX_LEN}"
        )
    if not text.strip():
        raise ValueError("the entry is blank")
    if text != text.strip():
        raise ValueError("the entry has padding at one end")
    for char in text:
        code = ord(char)
        if code < 0x20 or code == 0x7F:
            raise ValueError(f"control character U+{code:04X} in the entry")
        if char in BAD_SEPARATORS:
            raise ValueError(f"line separator U+{code:04X} in the entry")
    return text.encode("utf-8")


def _flush_directory(directory):
    """Push a directory entry to disk; ignore filesystems that refuse."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _drop_quietly(path):
    """Delete ``path`` if it is there; ignore it if it is already gone."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _sweep_scraps(directory):
    """Clear leftover ``.entry-*`` files from interrupted earlier runs.

    Only plain files are removed: a directory that happens to share
    the prefix is left alone. Each file gets a few tries with a short
    pause, in case a dying sibling process still holds it. A file that
    will not go away stays where it is -- it can never become
    ``entry.txt`` and must not fail the run.
    """
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(SCRAP_PREFIX):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        scrap = entry.path
        for _ in range(SCRAP_TRIES):
            try:
                os.unlink(scrap)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(SCRAP_PAUSE)
        else:
            _drop_quietly(scrap)


def _drain(fd, data):
    """Push every byte of ``data`` into ``fd``.

    One ``os.write`` call may accept fewer bytes than offered, so loop
    until the buffer is empty. A call that accepts nothing while data
    remains means no progress is possible and fails loudly instead of
    spinning forever. Returns the byte count written.
    """
    view = memoryview(data)
    sent = 0
    while sent < len(data):
        count = os.write(fd, view[sent:])
        if count == 0:
            raise OSError("os.write accepted 0 bytes with data remaining")
        sent += count
    return sent


class EntrySink:
    """Writes the entry file for one output directory.

    ``publish`` writes the bytes to a temporary file next to the
    target, flushes it, and renames it over ``entry.txt``; ``confirm``
    then reads the finished file back and checks it as published.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.target = self.directory / OUT_NAME

    def publish(self, data):
        """Atomically replace ``entry.txt`` with ``data`` at mode 0644."""
        fd, tmp_name = tempfile.mkstemp(prefix=SCRAP_PREFIX, dir=self.directory)
        tmp_path = Path(tmp_name)
        try:
            try:
                os.fchmod(fd, OUT_MODE)
                _drain(fd, data)
                if os.fstat(fd).st_size != len(data):
                    raise OSError(
                        "temporary file holds "
                        f"{os.fstat(fd).st_size} bytes, expected {len(data)}"
                    )
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp_path, OUT_MODE)
            os.replace(tmp_path, self.target)
            os.chmod(self.target, OUT_MODE)
            _flush_directory(self.directory)
        except BaseException:
            _drop_quietly(tmp_path)
            raise
        return self.target

    def confirm(self, data):
        """Read ``entry.txt`` back and check size, bytes, gate and mode.

        The size comes from ``stat`` first, then the bytes are
        compared, then the read-back bytes are decoded and run through
        the whole gate again -- so the file the scorer will read is the
        file that gets checked, not the constant in memory. A wrong
        mode fails too: the scorer runs as another user and must be
        able to read the file.
        """
        info = os.stat(self.target)
        if info.st_size != len(data):
            raise ValueError(
                f"entry.txt holds {info.st_size} bytes, expected {len(data)}"
            )
        with open(self.target, "rb") as handle:
            back = handle.read()
        if back != data:
            raise ValueError("entry.txt does not match what was written")
        try:
            published = back.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"entry.txt cannot be decoded as UTF-8: {exc}") from exc
        _check_text(published)
        mode = stat.S_IMODE(info.st_mode)
        if mode != OUT_MODE:
            raise ValueError(f"entry.txt mode is {mode:04o}, wanted 0644")


def run():
    """Write the entry and confirm it; 0 on success."""
    directory = _writable_dir(os.environ.get("MAKEFUN_OUTPUT_DIR"))
    _sweep_scraps(directory)
    data = _check_text(ENTRY_TEXT)
    sink = EntrySink(directory)
    sink.publish(data)
    sink.confirm(data)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as exc:  # noqa: BLE001 - one line on stderr, then exit 1
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
