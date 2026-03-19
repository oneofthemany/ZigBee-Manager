"""
Core package - ZigbeeService composed from focused mixin classes.

ZigbeeService inherits from all mixins. Each mixin owns a logical domain
and all methods reference `self` normally.

"""
from core.service import ZigbeeService
from core.polling import PollingScheduler

__all__ = ['ZigbeeService', 'PollingScheduler']