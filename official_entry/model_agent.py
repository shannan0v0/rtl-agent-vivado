"""Compatibility name for the canonical agent.model_agent module object."""

import sys as _sys
from agent import model_agent as _implementation

_sys.modules[__name__] = _implementation
