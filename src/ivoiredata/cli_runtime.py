from __future__ import annotations

import argparse

from . import __version__
from . import cli as base


_original_parser = base.parser


def parser():
    """Build the legacy CLI parser but source --version from package metadata.

    The historical CLI embedded ``0.8.4`` as a string literal, which allowed the
    package, image and VERSION file to move ahead while ``ivoiredata --version`` stayed
    stale.  Keep all existing subcommands untouched and replace only argparse's version
    action at runtime.
    """

    value = _original_parser()
    for action in getattr(value, "_actions", []):
        if isinstance(action, argparse._VersionAction):
            action.version = f"ivoiredata {__version__}"
    return value


base.parser = parser
main = base.main
