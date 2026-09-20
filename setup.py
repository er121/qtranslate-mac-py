"""py2app build script — packages QTranslateMac into a real, standalone
.app bundle. This is what lets a person install by dragging to Applications
and double-clicking, without ever installing Python, pip, or opening a
Terminal.

Build with (macOS only):

    pip install -r requirements.txt py2app
    python setup.py py2app

The resulting bundle is at dist/QTranslateMac.app. This is built and
smoke-launched for real on a genuine macOS GitHub Actions runner — see
.github/workflows/build-app.yml — not just assumed to work.
"""
from setuptools import setup

APP = ["launcher.py"]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        # Menu-bar-only app: no Dock icon, no application menu bar. This is
        # what makes it behave like a normal "utility that lives in the
        # menu bar" instead of popping up in the Dock and Cmd+Tab switcher.
        "LSUIElement": True,
        "CFBundleName": "QTranslateMac",
        "CFBundleDisplayName": "QTranslateMac",
        "CFBundleIdentifier": "com.qtranslatemac.app",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHumanReadableCopyright": "QTranslateMac",
    },
    "packages": [
        "rumps",
        "objc",
        "AppKit",
        "Foundation",
        "Quartz",
        "ApplicationServices",
        "deep_translator",
        "qtranslate_mac",
    ],
}

setup(
    app=APP,
    name="QTranslateMac",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
