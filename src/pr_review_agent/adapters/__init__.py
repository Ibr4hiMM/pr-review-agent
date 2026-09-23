from ..config import ProjectConfig
from .base import LanguageAdapter
from .dart import DartAdapter
from .python import PythonAdapter
from .typescript import TypeScriptAdapter

_ADAPTERS: dict[str, LanguageAdapter] = {
    "typescript": TypeScriptAdapter(),
    "python": PythonAdapter(),
    "dart": DartAdapter(),
}


def adapter_for(project: ProjectConfig) -> LanguageAdapter:
    return _ADAPTERS[project.language]
