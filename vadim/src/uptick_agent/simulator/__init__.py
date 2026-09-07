"""Lazy compatibility exports for the simulator adapters."""

from importlib import import_module

__all__ = ["SimulatorApiError", "SimulatorClient", "SimulatorEnvironment"]

_EXPORTS = {
    "SimulatorApiError": ("client", "SimulatorApiError"),
    "SimulatorClient": ("client", "SimulatorClient"),
    "SimulatorEnvironment": ("environment", "SimulatorEnvironment"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
