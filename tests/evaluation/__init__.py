"""Model-evaluation harness for issue #1129.

Scoped to this issue's role/step fixture matrix: a manually-triggered tool
for producing a checked-in results doc, not a general-purpose benchmarking
product and not a CI gate (it costs real money against a live provider).
See ``harness.py`` for the attempt/aggregation model and
``test_role_step_matrix.py`` for how to actually run it.
"""
