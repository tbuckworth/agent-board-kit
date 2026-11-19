"""The scripts in bin/ for tests: load_script runs one (it has no .py suffix) as a module, so a test can call its
functions. A test module imports isolate_state first: a script imports agentmail when it loads."""
import importlib.machinery
import importlib.util
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def load_script(name):
    """bin/NAME, loaded as a fresh module (named NAME with "_" for "-"); each call runs it anew."""
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod
