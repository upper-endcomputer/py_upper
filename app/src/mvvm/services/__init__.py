"""Service Layer - Injected capabilities (communication, repositories, backends).

Services are plain Python: they hold no Qt import and no ViewModel reference, so
the ViewModel that receives them stays testable without a window.
"""
from mvvm.services.greeting import GreetingService

__all__ = ['GreetingService']
