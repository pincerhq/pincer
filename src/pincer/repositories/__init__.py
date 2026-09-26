"""Per-domain repositories: data access over one session, no commits.

A domain is a module (`<domain>.py`) until it grows past ~300 lines or its
repositories stop being closely related; then it becomes a package whose
`__init__.py` re-exports the public names.
"""
