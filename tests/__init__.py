"""Azleem's offline test suite.

Runs without a network, without an API key, and without touching the desktop:

    python -m unittest discover -s tests -v

The one exception is tests/test_routing_live.py, which asks the real model what
tool it would pick. It is skipped unless AZLEEM_LIVE_TESTS=1 is set, and even
then it never lets a tool actually run.
"""
