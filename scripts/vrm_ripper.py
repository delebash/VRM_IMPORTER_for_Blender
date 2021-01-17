#!/usr/bin/env python3

"""
Copyright (c) 2018 iCyP
Released under the MIT license
https://opensource.org/licenses/mit-license.php

# noqa: INP001
"""

import json
import os
import sys
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
from os.path import dirname

sys.path.insert(0, dirname(dirname(__file__)))

# pylint: disable=wrong-import-position;
from io_scene_vrm.importer.vrm_load import parse_glb  # noqa: E402

# pylint: enable=wrong-import-position;


def exist_or_makedir(path: str) -> str:
    ripped_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "ripped")
    if not os.path.exists(ripped_dir):
        os.mkdir(ripped_dir)
    return ripped_dir


model_path = filedialog.askopenfilename(filetypes=[("", "*vrm")])
with open(model_path, "rb") as f:
    vrm_json, binary = parse_glb(f.read())
if messagebox.askyesno(message="write VRM.json?"):
    writedir = exist_or_makedir(model_path)
    writejsonpath = os.path.join(writedir, "vrm.json")
    with open(writejsonpath, "w") as json_file:
        json.dump(vrm_json, json_file, indent=4)
