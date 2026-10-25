"""The fixed Python 3.13 runtime uses stdlib zipfile.Path, never the <3.10 zipp branch.

Exclude this single optional import edge rather than globally excluding zipp:
global exclusion conflicts with PyInstaller's setuptools-vendoring alias hook.
"""
excludedimports = ["zipp"]
