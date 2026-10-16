#!/usr/bin/env python3
"""agent-desk CLI launcher. The code lives in the agent_desk package so Python caches its bytecode
(a script run directly is recompiled on every call, which matters for per-tool-use hooks)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # also under PYTHONSAFEPATH / -P

from agent_desk import main

sys.exit(main())
