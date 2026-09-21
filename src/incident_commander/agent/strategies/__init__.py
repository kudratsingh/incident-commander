"""Inference strategies for the investigation loop (plan 02 § 4, WP-0.2).

A strategy replaces ``investigation.py::_plan_next_step`` and never acts (ADR 0036). Nothing is
re-exported on purpose — ``config.Settings`` validates against :mod:`.names`, and a re-export
here would make that circular. Import the submodule you need.
"""
