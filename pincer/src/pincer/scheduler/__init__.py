"""
Pincer Scheduler Package.

Components:
- cron.py: CronScheduler — persistent cron-based task scheduler
- proactive.py: ProactiveAgent — morning briefing, custom actions
- triggers.py: EventTriggerManager — email/calendar/webhook reactive triggers
"""

from pincer.scheduler.cron import CronScheduler
from pincer.scheduler.proactive import ProactiveAgent
from pincer.scheduler.triggers import EventTriggerManager

__all__ = ["CronScheduler", "EventTriggerManager", "ProactiveAgent"]
