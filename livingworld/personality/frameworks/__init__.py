"""Animation framework adapters. Each adapter reads its framework's own installed files and maps its action ids onto
the framework-neutral activity groups of ``personality/intimacy.py``. Personality generation never imports these.

Only OStim Standalone is supported (``ostim.py``). SexLab, Flower Girls and others are not interchangeable with OStim
and would each need their own adapter implementing ``base.Framework``.
"""
