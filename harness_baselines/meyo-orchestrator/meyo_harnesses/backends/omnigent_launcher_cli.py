"""Internal config-only helper. Executed by the installed Omnigent CLI's Python."""
import contextlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys


def main():
    request = json.load(sys.stdin)
    sys.path.insert(0, request["entry_dir"])
    with contextlib.redirect_stdout(sys.stderr):
        import omnigent

        result = {"ok": True, "python": sys.executable, "prefix": sys.prefix,
                  "native_source": omnigent.__file__, "pythonpath": os.environ.get("PYTHONPATH", "")}
        if request["operation"] == "inspect":
            errors = []
            for module in ("python_socks", "six.moves", "dateutil.rrule", "omnigent.server.app"):
                try:
                    importlib.import_module(module)
                except Exception as exc:
                    errors.append(f"{module}:{type(exc).__name__}:{str(exc)[:120]}")
            from omnigent.runtime.harnesses import _HARNESS_MODULES
            result.update(runtime_errors=errors, sub_harnesses=sorted(_HARNESS_MODULES))
        elif request["operation"] == "launcher":
            import yaml
            from omnigent.cli import _materialize_harness_launcher_file

            original = _materialize_harness_launcher_file(
                harness=request["harness"], model=request["model"],
                system_prompt=None, client_tools="coding")
            try:
                raw = yaml.safe_load(original.read_text(encoding="utf-8"))
                raw["executor"]["auth"] = {"type": "api_key", "api_key": request["api_key"],
                                           "base_url": request["base_url"]}
                raw["executor"]["use_responses"] = True
                # Credentials are written only into the parent's private temporary dir.
                fd = os.open(request["output"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    yaml.safe_dump(raw, output, allow_unicode=True)
                result["output"] = request["output"]
            finally:
                # The original factory file never contained credentials. Avoid rmtree
                # of a path returned by a future/different native factory.
                original.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    original.parent.rmdir()
        else:
            raise ValueError("unknown internal native operation")
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            result["native_version"] = importlib.metadata.version("omnigent")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"native helper: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
