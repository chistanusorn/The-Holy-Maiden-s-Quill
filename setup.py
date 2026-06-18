import sys

from cx_Freeze import Executable, setup


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
    ],
    "include_files": [
        "icon.ico",
    ],
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
