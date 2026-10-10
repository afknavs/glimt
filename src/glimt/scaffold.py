"""`glimt bridge new` and `glimt bridge test`."""

from __future__ import annotations

import asyncio
import re
from importlib.resources import files

from . import config as config_module, conformance, paths


def new(name: str | None) -> str:
    if not name or not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", name):
        raise SystemExit("Usage: glimt bridge new <type>   (lowercase letters, digits, dashes)")
    folder = paths.user_bridges_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (name.replace("-", "_") + ".py")
    if target.exists():
        raise SystemExit(f"{target} already exists")
    class_name = "".join(part.capitalize() for part in name.split("-")) + "Bridge"
    template = files("glimt").joinpath("bridges", "_template.py").read_text()
    target.write_text(template.replace("TemplateBridge", class_name).replace('type = "template"', f'type = "{name}"')
                      .replace("bridges.template", f"bridges.{name}").replace('bridge = "template"', f'bridge = "{name}"'))
    return (f"Created {target}\n\n"
            f"1. Fill in the TODOs (the file explains each one; docs/bridges.md has the full contract).\n"
            f"2. Add to {paths.config_file()}:\n\n"
            f"     [bridges.{name}]\n\n"
            f"     [devices.my-light]\n     bridge = \"{name}\"\n     kind = \"light\"\n\n"
            f"3. glimt bridge test {name}          (read-only checks)\n"
            f"   glimt bridge test {name} --write  (also re-sends current values to time writes)\n"
            f"4. glimt restart")


def test(name: str | None, write: bool = False) -> int:
    if not name:
        raise SystemExit("Usage: glimt bridge test <type> [--write]")
    results = asyncio.run(conformance.check(name, config_module.load(), write=write))
    for result in results:
        print(result)
    failed = sum(not result.ok for result in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed" + ("" if failed else ". This bridge conforms."))
    return 1 if failed else 0
