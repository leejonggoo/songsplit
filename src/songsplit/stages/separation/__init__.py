from songsplit.stages.separation.base import SeparationEngine
from songsplit.stages.separation.demucs_engine import DemucsEngine
from songsplit.stages.separation.spleeter_engine import SpleeterEngine

_ENGINES: dict[str, type[SeparationEngine]] = {
    "demucs": DemucsEngine,
    "spleeter": SpleeterEngine,
}


def get_engine(name: str, **kwargs) -> SeparationEngine:
    try:
        cls = _ENGINES[name]
    except KeyError:
        raise ValueError(f"알 수 없는 분리 엔진: {name}. 사용 가능: {sorted(_ENGINES)}")
    return cls(**kwargs)


__all__ = ["SeparationEngine", "DemucsEngine", "SpleeterEngine", "get_engine"]
