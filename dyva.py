#!/usr/bin/env python3
import sys
from pathlib import Path

if not __package__:
    from dyva import main
else:
    from .dyva import main

if __name__ == "__main__": 
    main()
