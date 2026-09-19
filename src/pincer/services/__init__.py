"""Per-domain services: business logic and the unit-of-work boundary.

Services open `pincer.db.session.session_scope()` and call repositories. The
dependency direction is api/cli/voice/scheduler → services → repositories →
models → db; nothing here is imported by a repository or a model.
"""
