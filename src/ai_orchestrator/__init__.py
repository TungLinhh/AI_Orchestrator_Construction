"""Autonomous Company OS — a governed control plane and agent runtime.

The package is organised in layers. Dependencies point inwards only:

    api  ->  application  ->  domain
                  |
                  v
            infrastructure

`domain` imports nothing from this package. Everything else may.
"""

__version__ = "0.1.0"
