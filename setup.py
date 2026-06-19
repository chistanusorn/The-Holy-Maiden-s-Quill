import sys

from cx_Freeze import Executable, setup


import glob
credentials = glob.glob("translate-*.json")
credentials_file = credentials[0] if credentials else None

include_files = ["icon.ico"]
if credentials_file:
    include_files.append(credentials_file)

build_exe_options = {
    "packages": [
        "os",
        "sys",
        "mss",
        "qdarktheme",
        "pynput",
        "google.cloud.vision",
        "google.cloud.translate_v2",
        "google.api_core",
        "pythainlp",
    ],
    "include_files": include_files,
    "excludes": ["tkinter"],
}

base = None
if sys.platform == "win32":
    base = "Win32GUI"

setup(
    name="Philia Translator",
    version="1.0",
    description="On-screen game translator",
    options={"build_exe": build_exe_options},
    executables=[
        Executable(
            "main.py",
            base=base,
            target_name="Philia Translator.exe",
            icon="icon.ico",
        )
    ],
)
