"""Prove native binaries run from the declared closure with no PATH fallback."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    git, tmux, postgres, initdb, pg_ctl, uv = [Path(p).resolve() for p in sys.argv[1:]]
    runtime = git.parent.parent
    with tempfile.TemporaryDirectory() as temporary:
        env = {
            "HOME": temporary,
            "PATH": str(runtime / "bin"),
            "LD_LIBRARY_PATH": str(runtime / "lib"),
            "GIT_EXEC_PATH": str(runtime / "libexec/git-core"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LANG": "C",
            "TZ": "UTC",
        }
        for binary, args, expected in [
            (git, ["--version"], "git version 2.51.2"),
            (tmux, ["-V"], "tmux 3.6a"),
            (postgres, ["--version"], "postgres (PostgreSQL) 18.1"),
            (initdb, ["--version"], "initdb (PostgreSQL) 18.1"),
            (pg_ctl, ["--version"], "pg_ctl (PostgreSQL) 18.1"),
        ]:
            value = subprocess.check_output([binary, *args], env=env, text=True).strip()
            assert value == expected, value
        assert (
            subprocess.check_output([uv, "--version"], env=env, text=True).split()[1] == "0.12.17"
        )
        subprocess.run([git, "init", "--quiet", temporary], env=env, check=True)
        subprocess.run(
            [
                git,
                "-C",
                temporary,
                "-c",
                "user.name=Smoke",
                "-c",
                "user.email=smoke@invalid",
                "commit",
                "--allow-empty",
                "--quiet",
                "-m",
                "toolchain smoke",
            ],
            env=env,
            check=True,
        )
    print("PostgreSQL 18.1 / git 2.51.2 / tmux 3.6a: OK")


if __name__ == "__main__":
    main()
