"""Build and validate frontend releases without changing the serving directory."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import urlopen


def validate_build(release: Path) -> None:
    build = release / ".next"
    for name in ("BUILD_ID", "routes-manifest.json", "server/pages/500.html"):
        if not (build / name).is_file() or not (build / name).stat().st_size:
            raise RuntimeError(f"Incomplete frontend build: {name}")
    paths = json.loads((build / "server/app-paths-manifest.json").read_text())
    if "/admin/page" not in paths or "/login/page" not in paths:
        raise RuntimeError("Frontend build is missing required pages")
    for route, module in paths.items():
        if route.endswith("/page"):
            for name in (module, module.removesuffix(".js") + "_client-reference-manifest.js"):
                if not (build / "server" / name).is_file():
                    raise RuntimeError(f"Frontend build is missing {name}")


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"src", "href"} and value and value.startswith("/_next/static/"):
                self.paths.add(value)


def check_http(base: str) -> None:
    with urlopen(base + "/login", timeout=5) as response:
        if response.status != 200 or "text/html" not in response.headers.get("Content-Type", ""):
            raise RuntimeError("Login page failed its HTTP check")
        assets = Assets()
        assets.feed(response.read().decode())
    if not assets.paths:
        raise RuntimeError("Login page has no build assets")
    for path in sorted(assets.paths):
        with urlopen(base + path, timeout=5) as response:
            if response.status != 200 or "text/html" in response.headers.get("Content-Type", ""):
                raise RuntimeError("Frontend static asset failed its HTTP check")
            if not response.read(1):
                raise RuntimeError("Frontend static asset is empty")


def smoke(release: Path, node: str) -> None:
    validate_build(release)
    passed = False
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (release / "smoke.log").open("w") as log:
        process = subprocess.Popen(
            [node, str(release / "node_modules/next/dist/bin/next"), "start", "--hostname",
             "127.0.0.1", "--port", str(port)], cwd=release,
            env={**os.environ, "NODE_ENV": "production"}, stdout=log, stderr=log,
        )
        try:
            for _ in range(30):
                if process.poll() is not None:
                    break
                try:
                    check_http(f"http://127.0.0.1:{port}")
                    passed = True
                    return
                except (OSError, RuntimeError):
                    time.sleep(1)
            raise RuntimeError(f"Frontend smoke test failed; inspect {release / 'smoke.log'}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if passed:
                (release / "smoke.log").unlink(missing_ok=True)


def prepare(source: Path, release: Path, env_file: Path, node_bin: Path, sha: str) -> None:
    # A release is built at its permanent path: generated absolute paths stay valid.
    if source.resolve() == release.resolve() or release.exists():
        raise RuntimeError("Frontend release must be a new directory")
    shutil.copytree(source, release, ignore=shutil.ignore_patterns(".next", "node_modules", ".env*"))
    shutil.copyfile(env_file, release / ".env.local")
    (release / ".env.local").chmod(0o600)
    (release / ".release-sha").write_text(sha + "\n")
    env = {**os.environ, "PATH": str(node_bin) + os.pathsep + os.environ.get("PATH", ""),
           "GIT_COMMIT_SHA": sha, "NEXT_PUBLIC_RELEASE": sha, "NEXT_TELEMETRY_DISABLED": "1"}
    subprocess.run([str(node_bin / "npm"), "ci"], cwd=release, env=env, check=True)
    subprocess.run([str(node_bin / "npm"), "run", "build"], cwd=release, env=env, check=True)
    smoke(release, str(node_bin / "node"))
    # Root-led recovery and the ordinary devrimo deploy account both retain the
    # existing runtime owner's access to the copied private environment/cache.
    owner = env_file.stat()
    if os.geteuid() == 0:
        for root, dirs, files in os.walk(release):
            for path in [Path(root), *(Path(root) / name for name in dirs + files)]:
                os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)


def switch(current: Path, target: Path) -> None:
    """Called only after stopping the frontend; retain the previous directory."""
    validate_build(target)
    temporary = current.with_name(current.name + ".switch")
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError("Unfinished frontend switch requires inspection")
    temporary.symlink_to(target.resolve(), target_is_directory=True)
    try:
        if current.is_dir() and not current.is_symlink():
            raise RuntimeError("Move the initial frontend to its retained release path before switching")
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "validate", "smoke", "switch", "check-http"])
    parser.add_argument("--release", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--node-bin", type=Path, default=Path("/opt/devrimo/node/bin"))
    parser.add_argument("--sha")
    parser.add_argument("--current", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:3000")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.source, args.release, args.env_file, args.node_bin, args.sha)
    elif args.command == "validate":
        validate_build(args.release)
    elif args.command == "smoke":
        smoke(args.release, str(args.node_bin / "node"))
    elif args.command == "switch":
        switch(args.current, args.release)
    else:
        check_http(args.url)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as error:
        print(f"Frontend release check failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
