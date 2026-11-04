"""Check release identity and the exact distributions that will be uploaded."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from pathlib import Path
import tarfile
import tomllib
import zipfile

ASSETS = (
    "r2d2box/server.html",
    "r2d2box/static/r2d2box.js",
    "r2d2box/static/r2d2box.css",
    "r2d2box/static/vendor/marked.min.js",
    "r2d2box/static/vendor/purify.min.js",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    version = project["version"]
    if args.tag != f"v{version}":
        raise SystemExit("Tag must match pyproject.toml version")
    if Path("RELEASE-NOTES.md").read_text().splitlines()[0] != f"# r2d2box v{version}":
        raise SystemExit("Release notes must describe the version being published")
    if args.dist:
        wheel = args.dist / f"r2d2box-{version}-py3-none-any.whl"
        source = args.dist / f"r2d2box-{version}.tar.gz"
        if {p for p in args.dist.iterdir() if p.name != ".gitignore"} != {wheel, source}:
            raise SystemExit("dist must contain exactly the release wheel and sdist")
        with zipfile.ZipFile(wheel) as archive:
            for asset in ASSETS:
                if not archive.read(asset):
                    raise SystemExit(f"Empty wheel asset: {asset}")
            metadata = archive.read(f"r2d2box-{version}.dist-info/METADATA")
            entrypoints = archive.read(f"r2d2box-{version}.dist-info/entry_points.txt").decode()
            if "r2d2box = r2d2box.cli:main" not in entrypoints:
                raise SystemExit("Missing standalone entry point")
        with tarfile.open(source) as archive:
            prefix = f"r2d2box-{version}/"
            for asset in ASSETS:
                if not archive.getmember(prefix + "src/" + asset).size:
                    raise SystemExit(f"Empty source asset: {asset}")
            member = archive.extractfile(prefix + "PKG-INFO")
            assert member is not None
            source_metadata = member.read()
        for raw in [metadata, source_metadata]:
            parsed = BytesParser().parsebytes(raw)
            if parsed["Name"] != "r2d2box" or parsed["Version"] != version:
                raise SystemExit("Distribution identity does not match release")
            requirements = parsed.get_all("Requires-Dist", [])
            if "thinkeryzu-agent-proxy>=0.3.1" not in requirements:
                raise SystemExit("Missing PyPI agent-proxy runtime requirement")
    print(f"Validated r2d2box {args.tag}" + (" distributions" if args.dist else " metadata"))


if __name__ == "__main__":
    main()
