"""Entry point that py2app packages as the .app bundle's actual executable.

This is the only reason this file exists separately from
qtranslate_mac/macapp.py: py2app wants a top-level script to build from.
"""
from qtranslate_mac.macapp import main

if __name__ == "__main__":
    main()
