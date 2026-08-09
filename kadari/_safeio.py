"""Opening files that hold someone else's production data.

Every file this client writes is one of three things: the capture log (verbatim production
prompts and the answers a customer paid for), the submission bundle (those same prompts in
one archive), or a report derived from them. A bare ``open()`` creates all of them with the
mode the process umask happens to allow -- **0644 under 0755 on the ordinary default** --
which is readable by every other account on a shared host, by a sidecar sharing the volume,
and by a backup or monitoring agent running as a different uid. Nothing in the tool said so,
and the README's privacy section still described the data as merely "local".

So the writes go through here instead, and the rules are:

* **Owner-only, but only on files we create.** ``O_CREAT`` applies ``0o600`` at creation and
  a umask can only ever remove *more* bits, never add them back -- so a created file is
  always ``0o600``. An **existing** file keeps whatever mode it has, because a customer who
  deliberately set group-read on their own log is making a decision, not a mistake, and a
  capture library has no business overriding it.

* **Never through a symlink.** ``O_NOFOLLOW`` refuses to write through a link someone else
  planted at our path -- the difference between a log that stays where the customer put it
  and one quietly teed into an attacker-readable file.

* **Regular files only.** A FIFO at the log path makes ``open()`` block until a reader shows
  up, which means a capture side-channel can hang the request that triggered it. We open
  non-blocking, confirm the target is a regular file, and refuse anything else.

None of this may become a new way to break the host. Everything here raises ``OSError``
subclasses, and every caller on the hot path already treats an exception as "drop the record
and carry on" (``LiveRecorder.record`` fails open). A tightening that cannot be applied is a
dropped record at worst -- never an exception in someone's production call path.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

#: Owner read/write. See the module docstring for why this is not configurable per call.
FILE_MODE = 0o600
#: Owner read/write/execute -- a directory needs +x to be entered at all.
DIR_MODE = 0o700

# Absent on Windows, where symlink semantics differ entirely. `0` degrades to today's
# behaviour rather than raising, which is the right trade for a capture side-channel.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

_FLAGS = {
    "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
    "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    "wb": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
}


class UnsafeTarget(OSError):
    """The path is not something we are willing to write production data to.

    Raised for a symlink, a FIFO, a device, or a directory -- never for an ordinary
    permission or disk error, which propagate unchanged so they read the way they always
    have.
    """


def secure_makedirs(path: str | Path) -> None:
    """Create ``path`` and any missing parents, each owner-only.

    ``os.makedirs(mode=...)`` does not forward its mode to intermediate directories, so a
    nested log path would leave every level but the last world-readable. Each missing
    component is therefore created here explicitly. Directories that already exist are left
    exactly as they are.
    """
    path = Path(path)
    missing = []
    probe = path
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=DIR_MODE)
        except FileExistsError:          # a concurrent writer won the race; fine
            pass


def secure_open(path: str | Path, mode: str = "a", *, encoding: str | None = "utf-8"):
    """``open()`` for files that hold production data. See the module docstring.

    Supports the three modes this client actually uses: ``"a"``, ``"w"`` and ``"wb"``.
    """
    if mode not in _FLAGS:
        raise ValueError(f"secure_open does not support mode {mode!r}")
    path = Path(path)
    flags = _FLAGS[mode] | _NOFOLLOW | _NONBLOCK

    try:
        fd = os.open(path, flags, FILE_MODE)
    except OSError as exc:
        # ELOOP: O_NOFOLLOW refused a symlink. ENXIO: a FIFO with no reader -- which is
        # exactly the case that used to block forever. Both are "not a file we will write
        # to", not "the disk is full", so they get a message that says which.
        if exc.errno in (errno.ELOOP, errno.ENXIO):
            raise UnsafeTarget(
                exc.errno,
                f"refusing to write to {path}: it is a symlink or a pipe, not a regular "
                f"file. Point the log at a real path you control.") from exc
        raise

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeTarget(
                errno.EINVAL,
                f"refusing to write to {path}: not a regular file "
                f"(mode {stat.filemode(st.st_mode)}).")
        # O_NONBLOCK did its job at open() time; a regular file ignores it for read/write,
        # but clear it anyway so the descriptor behaves exactly like a plain open()'s.
        if _NONBLOCK:
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise

    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding=encoding)
