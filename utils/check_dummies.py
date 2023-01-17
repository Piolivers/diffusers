# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team.
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

import argparse
import os
import re


# All paths are set with the intent you should run this script from the root of the repo with the command
# python utils/check_dummies.py
PATH_TO_DIFFUSERS = "src/diffusers"

# Matches is_xxx_available()
_re_backend = re.compile(r"is\_([a-z_]*)_available\(\)")
# Matches from xxx import bla
_re_single_line_import = re.compile(r"\s+from\s+\S*\s+import\s+([^\(\s].*)\n")


DUMMY_CONSTANT = """
{0} = None
"""

DUMMY_CLASS = """
class {0}(metaclass=DummyObject):
    _backends = {1}

    def __init__(self, *args, **kwargs):
        requires_backends(self, {1})

    @classmethod
    def from_config(cls, *args, **kwargs):
        requires_backends(cls, {1})

    @classmethod
    def from_pretrained(cls, *args, **kwargs):
        requires_backends(cls, {1})
"""


DUMMY_FUNCTION = """
def {0}(*args, **kwargs):
    requires_backends({0}, {1})
"""


def find_backend(line):
    """Find one (or multiple) backend in a code line of the init."""
    backends = _re_backend.findall(line)
    if len(backends) == 0:
        return None

    return "_and_".join(backends)


def read_init():
    """Read the init and extracts PyTorch, TensorFlow, SentencePiece and Tokenizers objects."""
    with open(os.path.join(PATH_TO_DIFFUSERS, "__init__.py"), "r", encoding="utf-8", newline="\n") as f:
        lines = f.readlines()

    # Get to the point we do the actual imports for type checking
    line_index = 0
    backend_specific_objects = {}
    # Go through the end of the file
    while line_index < len(lines):
        # If the line contains is_backend_available, we grab all objects associated with the `else` block
        backend = find_backend(lines[line_index])
        if backend is not None:
            while not lines[line_index].startswith("else:"):
                line_index += 1
            line_index += 1
            objects = []
            # Until we unindent, add backend objects to the list
            while line_index < len(lines) and len(lines[line_index]) > 1:
                line = lines[line_index]
                single_line_import_search = _re_single_line_import.search(line)
                if single_line_import_search is not None:
                    objects.extend(single_line_import_search.groups()[0].split(", "))
                elif line.startswith(" " * 8):
                    objects.append(line[8:-2])
                line_index += 1

            if len(objects) > 0:
                backend_specific_objects[backend] = objects
        else:
            line_index += 1

    return backend_specific_objects


def create_dummy_object(name, backend_name):
    """Create the code for the dummy object corresponding to `name`."""
    if name.isupper():
        return DUMMY_CONSTANT.format(name)
    elif name.islower():
        return DUMMY_FUNCTION.format(name, backend_name)
    else:
        return DUMMY_CLASS.format(name, backend_name)


def create_dummy_files(backend_specific_objects=None):
    """Create the content of the dummy files."""
    if backend_specific_objects is None:
        backend_specific_objects = read_init()
    # For special correspondence backend to module name as used in the function requires_modulename
    dummy_files = {}

    for backend, objects in backend_specific_objects.items():
        backend_name = "[" + ", ".join(f'"{b}"' for b in backend.split("_and_")) + "]"
        dummy_file = "# This file is autogenerated by the command `make fix-copies`, do not edit.\n"
        dummy_file += "# flake8: noqa\n\n"
        dummy_file += "from ..utils import DummyObject, requires_backends\n\n"
        dummy_file += "\n".join([create_dummy_object(o, backend_name) for o in objects])
        dummy_files[backend] = dummy_file

    return dummy_files


def check_dummies(overwrite=False):
    """Check if the dummy files are up to date and maybe `overwrite` with the right content."""
    dummy_files = create_dummy_files()
    # For special correspondence backend to shortcut as used in utils/dummy_xxx_objects.py
    short_names = {"torch": "pt"}

    # Locate actual dummy modules and read their content.
    path = os.path.join(PATH_TO_DIFFUSERS, "utils")
    dummy_file_paths = {
        backend: os.path.join(path, f"dummy_{short_names.get(backend, backend)}_objects.py")
        for backend in dummy_files.keys()
    }

    actual_dummies = {}
    for backend, file_path in dummy_file_paths.items():
        if os.path.isfile(file_path):
            with open(file_path, "r", encoding="utf-8", newline="\n") as f:
                actual_dummies[backend] = f.read()
        else:
            actual_dummies[backend] = ""

    for backend in dummy_files.keys():
        if dummy_files[backend] != actual_dummies[backend]:
            if overwrite:
                print(
                    f"Updating diffusers.utils.dummy_{short_names.get(backend, backend)}_objects.py as the main "
                    "__init__ has new objects."
                )
                with open(dummy_file_paths[backend], "w", encoding="utf-8", newline="\n") as f:
                    f.write(dummy_files[backend])
            else:
                raise ValueError(
                    "The main __init__ has objects that are not present in "
                    f"diffusers.utils.dummy_{short_names.get(backend, backend)}_objects.py. Run `make fix-copies` "
                    "to fix this."
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix_and_overwrite", action="store_true", help="Whether to fix inconsistencies.")
    args = parser.parse_args()

    check_dummies(args.fix_and_overwrite)
