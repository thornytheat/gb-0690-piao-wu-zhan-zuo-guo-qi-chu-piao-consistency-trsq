import hashlib, json
from datetime import datetime, timezone
from app.config import RULE_VERSION


def digest(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def make(raw, params, base=None, root=None, revision=None, supplement=None):
    """Build the immutable batch manifest.

    A plain batch is identified by its input+params+rules.  A supplement is
    identified by its lineage root, its own payload, params and rules: retrying
    the same supplement request resolves to the same batch id and never adds a
    revision, even when the base has since advanced to the version produced by
    that very supplement.  A different supplement yields a distinct version.
    """
    content_digest = digest(raw)
    params_key = json.dumps(params, sort_keys=True, ensure_ascii=False)
    if base:
        supplement_digest = digest(supplement or b"")
        key = "\x1f".join((str(root), supplement_digest, params_key,
                           RULE_VERSION, "supplement"))
    else:
        key = content_digest + params_key + RULE_VERSION
    result = {"run_id": hashlib.sha256(key.encode()).hexdigest()[:20],
              "sha256": content_digest, "rules": RULE_VERSION, "params": params,
              "created": datetime.now(timezone.utc).isoformat(), "bytes": len(raw)}
    if base:
        result.update({"base": base, "root": root, "revision": revision,
                       "supplement_sha256": digest(supplement or b"")})
    return result
