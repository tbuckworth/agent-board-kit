"""What the commands in bin/ share: their argument parser, and printing a result as JSON or for people."""
import argparse
import json


def parser(doc, prog):
    """An ArgumentParser for the command `prog` whose script docstring is `doc`: the docstring's first paragraph is
    the description, and --help ends with the whole docstring."""
    return argparse.ArgumentParser(prog=prog, description=doc.split("\n\n")[0],
                                   formatter_class=argparse.RawDescriptionHelpFormatter, epilog=doc)


def out(obj, as_json, human):
    """Print `obj` on stdout: as indented JSON (non-ASCII kept as it is, anything else JSON lacks as its str()) when
    `as_json`, else through human(obj)."""
    if as_json:
        print(json.dumps(obj, indent=1, ensure_ascii=False, default=str))
    else:
        human(obj)
