"""Per-domain repositories: data access over one session, no commits.

A domain is a module (`<domain>.py`) until it holds more than one repository
class or grows past ~300 lines; then it becomes a package whose `__init__.py`
re-exports the public names.
"""
