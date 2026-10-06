"""Entry of the Windows exe: the window (``--background``: started at login, no window yet),
unless a command is given (python -m stzb_warroom ...)."""

import sys

from stzb_warroom.__main__ import main

args = sys.argv[1:]
main(["gui", *args] if not args or args == ["--background"] else args)
