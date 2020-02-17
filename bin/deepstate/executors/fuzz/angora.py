#!/usr/bin/env python
# Copyright (c) 2019 Trail of Bits, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import json
import logging
import argparse
import subprocess

from typing import ClassVar, List, Dict, Optional, Any

from deepstate.core import FuzzerFrontend, FuzzFrontendError


L = logging.getLogger("deepstate.frontend.angora")
L.setLevel(os.environ.get("DEEPSTATE_LOG", "INFO").upper())


class Angora(FuzzerFrontend):

  # these classvars are set under the assumption that $ANGORA_PATH is set to the built source
  NAME: ClassVar[str] = "Angora"
  SEARCH_DIRS: ClassVar[List[str]] = ["bin", "clang+llvm", "tools"]
  EXECUTABLES: ClassVar[Dict[str,str]] = {"FUZZER": "angora_fuzzer",
                                          "COMPILER": "angora-clang++",
                                          "GEN_LIB_ABILIST": "gen_library_abilist.sh"
                                          }


  @classmethod
  def parse_args(cls) -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
      description="Use Angora as a backend for DeepState.")

    # Other compilation arguments
    compile_group = parser.add_argument_group("compilation and instrumentation arguments")
    compile_group.add_argument("--ignore_calls", type=str,
      help="Path to static/shared libraries (colon seperated) for functions to skip (blackbox) for taint analysis.")

    # Angora-specific test execution options
    parser.add_argument("taint_binary", nargs="?", type=str,
      help="Path to binary compiled with taint tracking.")

    cls.parser = parser
    super(Angora, cls).parse_args()


  def compile(self) -> None: # type: ignore
    """
    Compilation interface provides extra support for generating taint policy for
    blacklisted ABI calls with DFsan.
    """

    env: Dict[str, str] = os.environ.copy()

    # generate ignored functions output for taint tracking
    # set envvar to file with ignored lib functions for taint tracking
    if self.ignore_calls: # type: ignore

      libpath: List[str] = self.ignore_calls.split(":") # type: ignore
      L.debug(f"Ignoring library objects: {libpath}")

      out_file: str = "abilist.txt"

      # TODO(alan): more robust library check
      ignore_bufs: List[bytes] = []
      for path in libpath:
        if not os.path.isfile(path):
          raise FuzzFrontendError(f"Library `{path}` to skip (blackbox) is not a valid library path.")

        # instantiate command to call, but store output to buffer
        cmd: List[str] = [self.EXECUTABLES["GEN_LIB_ABILIST"], path, "discard"]
        L.debug(f"Compilation command: {cmd}")

        out: bytes = subprocess.check_output(cmd)
        ignore_bufs += [out]


      # write all to final out_file
      with open(out_file, "wb") as f:
        for buf in ignore_bufs:
          f.write(buf)

      # set envvar for fuzzer compilers
      env["ANGORA_TAINT_RULE_LIST"] = os.path.abspath(out_file)


    # make a binary with light instrumentation
    fast_path: str = "/usr/local/lib/libdeepstate_fast.a"
    L.debug(f"Static library path: {fast_path}")

    fast_flags: List[str] = ["-ldeepstate_fast"]
    if self.compiler_args:
      fast_flags += [arg for arg in self.compiler_args.split(" ")]
    L.info(f"Compiling {self.compile_test} for {self.name} with light instrumentation")
    super().compile(fast_path, fast_flags, self.out_test_name + ".fast", env=env)

    # initialize envvar for instrumentation framework
    if self.mode == "pin": # type: ignore
      env["USE_PIN"] = "1"
    else:
      env["USE_TRACK"] = "1"

    # make a binary with taint tracking information
    taint_path: str = "/usr/local/lib/libdeepstate_taint.a"
    L.debug(f"Static library path: {taint_path}")

    taint_flags: List[str] = ["-ldeepstate_taint"]
    if self.compiler_args:
      taint_flags += [arg for arg in self.compiler_args.split(' ')]
    L.info(f"Compiling {self.compile_test} for {self.name} with taint tracking")
    super().compile(taint_path, taint_flags, self.out_test_name + ".taint", env=env)


  def pre_exec(self):
    super().pre_exec()

    # since base method checks for self.binary by default
    if not self.taint_binary:
      self.parser.print_help()
      raise FuzzFrontendError(f"Must provide taint binary for {self.name}.")

    if not os.path.exists(self.taint_binary):
      raise FuzzFrontendError("Taint binary doesn't exist")

    if not self.input_seeds:
      raise FuzzFrontendError(f"Must provide -i/--input_seeds option for {self.name}.")

    if not os.path.exists(self.input_seeds):
      raise FuzzFrontendError(f"Input seeds dir (`{self.input_seeds}`) doesn't exist.")

    if len(os.listdir(self.input_seeds)) == 0:
      raise FuzzFrontendError(f"No seeds present in directory `{self.input_seeds}`.")

    if self.blackbox == True:
      raise FuzzFrontendError(f"Blackbox fuzzing is not supported by {self.name}.")

    if self.dictionary:
      L.error(f"{self.name} can't use dictionaries.")

    # require output directory
    if not self.output_test_dir:
      raise FuzzFrontendError("Must provide -o/--output_test_dir.")

    if os.path.exists(self.output_test_dir):
      raise FuzzFrontendError(f"Remove previous output directory (`{self.output_test_dir}`) before running {self.name}.")


  @property
  def cmd(self):
    cmd_list: List[str] = list()

    # guaranteed arguments
    cmd_list.extend([
      "--mode", "llvm",  # TODO, add pin support
      "--track", os.path.abspath(self.taint_binary),
      "--memory_limit", str(self.mem_limit),
      "--output", self.output_test_dir  # auto-create, not reusable
    ])

    for key, val in self.fuzzer_args:
      if len(key) == 1:
        cmd_list.append('-{}'.format(key))
      else:
        cmd_list.append('--{}'.format(key))
      if val is not None:
        cmd_list.append(val)

    # optional arguments:
    # required, if provided: not auto-create and require any file inside
    if self.input_seeds:
      cmd_list.extend(["--input", self.input_seeds])

    if self.exec_timeout:
      cmd_list.extend(["--time_limit", str(self.exec_timeout / 1000)])

    # autodetect
    cmd_list.append("--disable_exploitation")

    return self.build_cmd(cmd_list)


  @property
  def stats(self) -> Optional[Dict[str, str]]:
    """
    Parses Angora output JSON config to dict for reporting.
    """
    stat_file: str = self.output_test_dir + "/chart_stat.json"

    if not hasattr(self, "prev_stats"):
      self.prev_stats: Optional[Dict[str, str]] = None

    try:
      with open(stat_file, "r") as handle:
        stats: Optional[Dict[str, str]] = json.loads(handle.read())
        self.prev_stats = stats

    # fallback on initially parsed stats if failed to decode
    except json.decoder.JSONDecodeError:
      stats = self.prev_stats

    return stats


  def reporter(self) -> Optional[Dict[str, Any]]:

    # included to silence mypy error
    if self.stats is None:
      return None

    return dict({
      "Execs Done": self.stats["num_exec"],
      "Unique Crashes": self.stats["num_crashes"],
      "Unique Hangs": self.stats["num_hangs"],
    })


def main():
  fuzzer = Angora(envvar="ANGORA_HOME")
  return fuzzer.main()


if __name__ == "__main__":
  exit(main())
