"""Allow `python -m core.warp` via `python -m core` → warp default? No.

Use: python -m core.warp
This package __main__ points at warp CLI for convenience: python -m core …
"""

from .warp import main

raise SystemExit(main())
