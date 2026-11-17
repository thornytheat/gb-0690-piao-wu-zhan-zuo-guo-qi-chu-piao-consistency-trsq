from importlib import import_module
from time import perf_counter
from app.config import STAGES, DEFAULTS, SAMPLES
from app.manifest import make
from app.normalize import serial, decode
from app.errors import DomainError
from app import store


def run(raw=None, params=None, stop=7, persist=True, base=None, root=None, revision=None,
        content=None, supplement=None):
    if raw is None:
        raw = (SAMPLES / "dirty.csv").read_bytes()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(params, (dict, type(None))):
        raise DomainError("PARAMS", "参数必须是对象")
    actual = {**DEFAULTS, **(params or {})}
    t = perf_counter()
    ctx = {"source": raw, "params": actual, "stages": [], "quarantine": []}
    for stage in range(1, stop + 1):
        mod = import_module(f"app.modules.m{stage}_{STAGES[stage - 1]}.service")
        ctx = mod.execute(ctx)
    ctx.pop("source", None)
    ctx.pop("raw", None)
    result = serial(ctx)
    result["manifest"] = make(raw, actual, base=base, root=root, revision=revision,
                              supplement=supplement)
    result["elapsed_ms"] = round((perf_counter() - t) * 1000, 3)
    result["version"] = 1
    result["review"] = "待复核"
    if persist and stop == 7:
        if content is None:
            try:
                content = decode(raw)[0]
            except DomainError:
                content = None
        return store.save(result, content=content)
    return result
