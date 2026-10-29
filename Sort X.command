#!/bin/bash
# Double-click this file in Finder to open Sort X.
# The app opens in its own window. This Terminal window can be ignored;
# it closes the app's helper when you close the Sort X window.
cd "$(dirname "$0")"
python3 file_sorter.py --gui
