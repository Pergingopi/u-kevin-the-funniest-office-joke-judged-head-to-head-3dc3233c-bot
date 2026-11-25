#!/usr/bin/env python3
"""The Funniest Office Joke, Judged Head to Head -- make.fun entry.

Writes one line to ``$MAKEFUN_OUTPUT_DIR/entry.txt``: a single joke
about office life, one short line, and that line is the entire entry.

The program is deliberately boring on purpose: no randomness, no clock,
no network, no third-party code -- Python 3.13 standard library only.
Every run writes the exact same bytes, so the sealed scorer sees the
same valid entry no matter who runs it or when.

The entry itself is a real joke with a setup and a turn. The setup
names the one meeting nobody ever skips -- the fire drill -- and then
escalates in three beats (everyone on time, everyone leaves together,
nobody asks for the slides), each beat true of a fire drill and
impossible of a real meeting. The punchline lands last, on the most
recognisable office ritual of all: asking for the slides. Anyone who
has worked in an office knows all three beats at once, which is what
the judge's brief rewards.

Three alternatives were compared against the judge's brief and lost:

* A, printer (110ch): "The printer only jams on the page I need, on
  the day I need it, in front of the one person I've been avoiding."
  States a complaint plainly with no turn -- the ending is flat, and
  the reader sees it coming from the first clause.
* B, meeting-about-meeting (114ch): "We held a meeting to plan the
  meeting about the meeting, and the only action item was to schedule
  another meeting." The punchline restates the setup; the circular
  repetition *is* the joke, so there is no surprise left for the end.
* C, reply-all (114ch): "I hit reply-all to say 'please remove me
  from this thread' and now seventy people are debating whether I
  meant it." Reply-all is the most cliched office joke in circulation,
  and the ending is convoluted -- "debating whether I meant it" lands
  soft next to the fire drill's three-beat escalation.

The kept line (116ch) wins on every criterion: one universal setup,
one turn, a punchline last, every word earning its place.

Output is written atomically (temporary file in the same directory,
fsync, rename, directory fsync) with mode 0644, then read back and
re-verified so a truncated or mistyped write is reported instead of
silently shipped.

Reviser notes (revision of the builder's first version):

* ``os.write`` may write fewer bytes than requested -- a short write
  the builder never checked for. Writes now loop until every byte is
  out (``_write_all``), treat a zero-byte write as a failure, and
  confirm the temporary file's size with ``fstat`` before the rename,
  so a truncated write can no longer slip through.
* ``resolve_output_dir`` used to hand any string straight to
  ``makedirs``: a relative path silently wrote somewhere the caller
  did not mean, a path blocked by an existing *file* died with a bare
  error, and ``MAKEFUN_OUTPUT_DIR=/`` would have swept the root
  directory's stale temporaries. It now normalises to an absolute
  path, rejects the filesystem root outright, raises a clear error
  when a file blocks the directory, and checks the directory is
  actually writable before anything is written.
* ``cleanup_stale_temporaries`` used to call ``unlink`` on anything
  whose name matched the prefix -- including a directory, which it
  then pointlessly retried three times with sleeps. It now scans with
  ``os.scandir`` and only touches regular files.
* ``verify_written_file`` used to compare bytes and mode only. It
  now also checks the file size from ``stat`` first, decodes the
  read-back bytes as UTF-8, and re-runs the full gate validation on
  the *published* text -- so the file the scorer will read is checked
  as published, not just the in-memory constant it was written from.
* Failures now exit 1 with one ``error: ...`` line on stderr instead
  of an unhandled traceback, while stdout stays silent so runs stay
  byte-identical.
"""

import os
import stat
import sys
import tempfile
import time

ENTRY = (
    "The fire drill is the only meeting where everyone shows up on "
    "time, leaves together, and nobody asks for the slides."
)

# The gate allows 1 to 200 characters; keep the constant next to the
# check so the two can never drift apart unnoticed.
MIN_CHARS = 1
MAX_CHARS = 200
TARGET_NAME = "entry.txt"
TARGET_MODE = 0o644
# Stale ".entry-*" temporaries left by an interrupted earlier run are
# swept before writing, with a bounded retry for a busy filesystem.
STALE_TMP_PREFIX = ".entry-"
STALE_CLEANUP_RETRIES = 3
STALE_CLEANUP_DELAY_SECONDS = 0.05
# Characters that would split the entry into more than one line, or
# otherwise break the single-line rule, even though the sealed gate
# (character count + not-whitespace-only) would not catch them.
LINE_SEPARATORS = ("\u0085", "\u2028", "\u2029")


def resolve_output_dir():
    """Return the directory that receives ``entry.txt``.

    ``MAKEFUN_OUTPUT_DIR`` wins when it names a non-blank directory;
    a blank or whitespace-only value falls back to ``/output`` instead
    of crashing on a relative or empty path. A relative value is
    resolved against the current working directory and normalised to
    an absolute path, so the write can never land somewhere the
    caller did not mean. The filesystem root is rejected outright --
    sweeping or writing there is never what a caller wants. The
    directory is created (parents included) so the write never fails
    on a missing folder; a path blocked by an existing file, or a
    directory that is not writable, raises a clear error here instead
    of failing halfway through the write.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR") or ""
    out_dir = raw.strip() or "/output"
    if not os.path.isabs(out_dir):
        out_dir = os.path.abspath(out_dir)
    out_dir = os.path.normpath(out_dir)
    if out_dir == os.path.abspath(os.sep):
        raise ValueError("output directory must not be the filesystem root")
    if os.path.lexists(out_dir) and not os.path.isdir(out_dir):
        raise ValueError(
            f"output directory {out_dir!r} is blocked by an existing file"
        )
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.isdir(out_dir):
        raise ValueError(f"output path {out_dir!r} is not a directory")
    if not os.access(out_dir, os.W_OK):
        raise ValueError(f"output directory {out_dir!r} is not writable")
    return out_dir


def validate_entry(text):
    """Check the entry against the sealed scorer's gate -- and the
    single-line rule the gate alone does not enforce.

    Raises ``ValueError`` with a specific message on the first problem
    found, so a bad entry fails loudly here rather than producing an
    invalid file: 1-200 characters, not only whitespace, free of
    control characters (a stray newline, tab or DEL would break the
    single-line rule), free of Unicode line separators, and with no
    leading or trailing whitespace (padding would waste the tight
    character budget and could be trimmed by a reader).
    """
    if not isinstance(text, str):
        raise ValueError("entry is not a string")
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        raise ValueError(
            f"entry is {len(text)} characters, gate needs "
            f"{MIN_CHARS}-{MAX_CHARS}"
        )
    if not text.strip():
        raise ValueError("entry is only whitespace")
    if text != text.strip():
        raise ValueError("entry has leading or trailing whitespace")
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise ValueError(f"control character U+{code:04X} in entry text")
        if ch in LINE_SEPARATORS:
            raise ValueError(f"line separator U+{code:04X} in entry text")


def _fsync_dir(path):
    """Flush a directory entry to disk; best effort on filesystems
    that do not support directory fsync."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _remove_quietly(path):
    """Delete a file if it exists; ignore the error if it is already
    gone or otherwise cannot be removed."""
    try:
        os.unlink(path)
    except OSError:
        pass


def cleanup_stale_temporaries(out_dir):
    """Remove leftover ``.entry-*`` files from interrupted runs.

    Only regular files are touched: a directory (or anything else)
    that happens to share the prefix is left alone instead of being
    retried pointlessly. Each candidate is retried a few times with a
    short pause, because a just-killed sibling process may still hold
    the file briefly. A file that still will not go away is left in
    place: it is harmless (it never becomes ``entry.txt``) and must
    not fail the run.
    """
    try:
        entries = list(os.scandir(out_dir))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(STALE_TMP_PREFIX):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        stale = entry.path
        for _attempt in range(STALE_CLEANUP_RETRIES):
            try:
                os.unlink(stale)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(STALE_CLEANUP_DELAY_SECONDS)
        else:
            _remove_quietly(stale)


def _write_all(fd, data):
    """Write every byte of ``data`` to ``fd``.

    A single ``os.write`` call may write fewer bytes than requested
    (a short write); loop until the whole buffer is out. A write of
    zero bytes with data still pending means the descriptor can make
    no progress and is treated as a failure, not spun on forever.
    Returns the number of bytes written.
    """
    view = memoryview(data)
    written = 0
    while written < len(data):
        n = os.write(fd, view[written:])
        if n == 0:
            raise OSError("short write: os.write wrote 0 bytes")
        written += n
    return written


def write_entry_atomic(out_dir, data):
    """Write ``data`` to ``entry.txt`` atomically with mode 0644.

    A temporary file is created in the same directory (so the rename
    cannot cross filesystems), given mode 0644, written in full
    (short writes looped over, size confirmed with ``fstat`` before
    the rename), fsynced, and renamed over the target; the directory
    is then fsynced so the rename itself is durable. On any failure
    the temporary file is removed and the exception is re-raised.
    """
    target = os.path.join(out_dir, TARGET_NAME)
    fd, tmp_path = tempfile.mkstemp(prefix=STALE_TMP_PREFIX, dir=out_dir)
    try:
        try:
            os.fchmod(fd, TARGET_MODE)
            _write_all(fd, data)
            if os.fstat(fd).st_size != len(data):
                raise OSError(
                    "temporary file size "
                    f"{os.fstat(fd).st_size} != {len(data)} bytes written"
                )
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp_path, TARGET_MODE)
        os.replace(tmp_path, target)
        os.chmod(target, TARGET_MODE)
        _fsync_dir(out_dir)
    except BaseException:
        _remove_quietly(tmp_path)
        raise
    return target


def verify_written_file(target, data):
    """Read the finished file back and confirm size, bytes, gate and
    mode -- on the published file, not the in-memory constant.

    The size is checked from ``stat`` first (a cheap short-write
    detector), then the bytes are compared, then the read-back bytes
    are decoded as UTF-8 and the full gate validation is re-run on
    that decoded text, so a truncation, a mistyped write or an
    encoding slip is raised instead of reported as success. A wrong
    mode means the file would not be readable where the scorer runs
    it; that is raised too.
    """
    info = os.stat(target)
    if info.st_size != len(data):
        raise ValueError(
            f"entry.txt is {info.st_size} bytes, expected {len(data)}"
        )
    with open(target, "rb") as fh:
        back = fh.read()
    if back != data:
        raise ValueError("read-back verification failed")
    try:
        published = back.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"entry.txt is not valid UTF-8: {exc}") from exc
    validate_entry(published)
    mode = stat.S_IMODE(info.st_mode)
    if mode != TARGET_MODE:
        raise ValueError(f"entry.txt mode is {mode:04o}, expected 0644")


def main():
    out_dir = resolve_output_dir()
    cleanup_stale_temporaries(out_dir)
    validate_entry(ENTRY)
    data = ENTRY.encode("utf-8")
    target = write_entry_atomic(out_dir, data)
    verify_written_file(target, data)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - report, then exit 1
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
