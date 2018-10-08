#!/usr/bin/env python3
import os
import subprocess
from pathlib import Path

import click

VENVS_DIR = Path(os.getenv("SINGER_VENVS_DIR", "/venvs"))
RUN_DIR = Path(os.getenv("SINGER_RUN_DIR"))
TAP_CONFIG_DIR = Path(os.getenv("SINGER_TAP_CONFIG_DIR", "/etc/singer/tap"))
TARGET_CONFIG_DIR = Path(os.getenv("SINGER_TARGET_CONFIG_DIR", "/etc/singer/target"))


def envsubst(src: Path, dst: Path):
    with src.open() as i, \
         dst.open("w+") as o:
        subprocess.Popen(["envsubst"], stdin=i, stdout=o)


def file_has_data(file: Path):
    return file.exists() and file.stat().st_size > 0


class SingerRunner():
    tap_files = {
        'config': RUN_DIR.joinpath("tap.config.json"),
        'catalog': RUN_DIR.joinpath("tap.properties.json"),
        'state': RUN_DIR.joinpath("state.json"),
    }

    target_files = {
        'config': RUN_DIR.joinpath("target.config.json"),
        'state': RUN_DIR.joinpath("new_state.json"),
    }

    def __init__(self, **config):
        self.tap_output_path = config.get("tap_output_path")


    def exec_path(self, name) -> Path:
        return VENVS_DIR.joinpath(name, "bin", name)


    def prepare(self, tap: str, target: str):
        config_files = {
            self.tap_files['config']: TAP_CONFIG_DIR.joinpath(f"{tap}.config.json"),
            self.tap_files['catalog']: TAP_CONFIG_DIR.joinpath(f"{tap}.properties.json"),
            self.target_files['config']: TARGET_CONFIG_DIR.joinpath(f"{target}.config.json"),
        }

        for dst, src in config_files.items():
            envsubst(src, dst)


    def invoke(self, tap: str, target: str):
        tap_args = [
            self.exec_path(tap),
            "-c", self.tap_files['config'],
            "--catalog", self.tap_files['catalog'],
        ]

        if file_has_data(self.tap_files['state']):
            tap_args += ["--state", self.tap_files['state']]

        tee_args = [
            "tee",
        ]

        if self.tap_output_path:
            tee_args += [self.tap_output_path]

        target_args = [
            self.exec_path(target),
            "-c", self.target_files['config'],
        ]

        p_target = subprocess.Popen(map(str, target_args),
                                    stdin=subprocess.PIPE,
                                    stdout=self.target_files['state'].open("w+"))

        p_tee = subprocess.Popen(tee_args,
                                 stdin=subprocess.PIPE,
                                 stdout=p_target.stdin)

        p_tap = subprocess.Popen(map(str, tap_args),
                                 stdout=p_tee.stdin)

        # Polling would probably be better
        tap_code = p_tap.wait()
        target_code = p_target.wait()

        if tap_code != 0:
            raise f"Tap exited with {tap_code}"

        if target_code != 0:
            raise f"Target exited with {target_code}"

    def run(self, tap: str, target: str):
        self.prepare(tap, target)
        self.invoke(tap, target)
        self.bookmark()

    def bookmark(self):
        if not file_has_data(self.target_files['state']):
            return

        self.target_files['state'].replace(self.tap_files['state'])


@click.command()
@click.argument("tap")
@click.argument("target")
def main(tap, target):
    runner = SingerRunner(tap_output_path=os.getenv("TAP_OUTPUT"))
    runner.run(tap, target)


if __name__ == '__main__':
    main()