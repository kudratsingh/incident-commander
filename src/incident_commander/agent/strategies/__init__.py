"""Inference strategies for the investigation loop (plan 02 § 4, WP-0.2).

A strategy replaces one call, ``investigation.py::_plan_next_step``. The ``FIX_MAP`` gate, the
0.7 threshold and the ADR-0009 re-probe stay there, because a strategy proposes and never acts
(ADR 0036). **Nothing is re-exported here on purpose:** ``config.Settings`` validates
``INFERENCE_STRATEGY`` against :mod:`.names`, so re-exporting :mod:`.baseline` would make that
a circular import. Import the submodule you need.
"""
