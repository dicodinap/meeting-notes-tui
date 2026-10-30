#!/usr/bin/env python3
"""Entry point for Meeting Notes application."""

import sys

if __name__ == "__main__":
    # The worker doesn't need the TUI, so skip importing it
    if '--worker' in sys.argv or '--add' in sys.argv:
        from meeting_notes.worker import main
        sys.exit(main())

    from meeting_notes.app import run

    # Parse args for --dev flag
    dev_mode = '--dev' in sys.argv
    
    run(dev_mode=dev_mode)
