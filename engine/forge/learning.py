"""Evidence-bound scoped playbook adaptation. This does not train model weights."""
from collections import defaultdict
import hashlib
from pathlib import Path
import re

from .core import ForgeError, atomic_json, canonical, load_json, text


CAVEATS = [
    "Agent playbook adaptation changes instructions, not model weights.",
    "Hashes bind recorded files; source honesty and semantic task independence are not proved.",
    "Paired regression evidence supports its recorded scope, not general project superiority.",
    "No customer-demand, business-viability or universal-best claim follows from promotion.",
]
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_/+=-]{12,}"),
]
EPISODE_FIELDS = {"task_id", "split", "scope", "lesson", "baseline_pass",
                  "candidate_pass", "evidence", "consent", "origin", "provenance"}
REQUIRED_FIELDS = EPISODE_FIELDS - {"provenance"}


def _reject_secrets(value):
    if any(pattern.search(value) for pattern in SECRET_PATTERNS):
        raise ForgeError("Possible secret in learning data; redact and review before training")


def _scope(value):
    return text(value, "learning scope", 200)


def _regular_file(path, label):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ForgeError("Invalid " + label + " path")
    value = Path(path)
    if not value.is_absolute():
        raise ForgeError(label + " path must be absolute")
    # This is validation, not protection from an adversarial filesystem race.
    if any(component.is_symlink() for component in [value, *value.parents]):
        raise ForgeError(label + " path must not contain symlinks")
    if not value.is_file():
        raise ForgeError(label + " must be a regular file")
    return value


def _file_hash(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _evidence(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ForgeError("Each episode needs 1..50 evidence files")
    found = set()
    verified = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ForgeError("Evidence must contain only path and sha256")
        value = item["sha256"]
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ForgeError("Invalid evidence SHA-256")
        path = _regular_file(item["path"], "Evidence")
        normalized = str(path.resolve())
        if normalized in found:
            raise ForgeError("Duplicate evidence file in episode")
        found.add(normalized)
        if _file_hash(path) != value:
            raise ForgeError("Evidence hash mismatch: " + normalized)
        # Scan every byte with chunk overlap; this remains heuristic secret
        # detection and does not replace human data review.
        with path.open("rb") as source:
            tail = b""
            for block in iter(lambda: source.read(1024 * 1024), b""):
                window = tail + block
                _reject_secrets(window.decode("utf-8", errors="ignore"))
                tail = window[-1024:]
        verified.append({"path": normalized, "sha256": value})
    return verified


def _provenance(value):
    if not isinstance(value, dict) or set(value) - {"source", "licence", "url"}:
        raise ForgeError("Invalid episode provenance")
    for field in ("source", "licence"):
        text(value.get(field), "provenance " + field, 2000)
    if "url" in value:
        url = text(value["url"], "provenance URL", 4000)
        if not url.startswith("https://"):
            raise ForgeError("External provenance URL must use HTTPS")
        if value["licence"].strip().lower() in {"unknown", "tbd", "none"}:
            raise ForgeError("External data needs reviewed usage rights")
    return dict(value)


def _read_dataset(dataset_path):
    path = _regular_file(str(Path(dataset_path).absolute()), "Dataset")
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ForgeError("Learning dataset exceeds 10 MiB")
    raw_hash = _file_hash(path)
    value = load_json(path)
    if not isinstance(value, dict) or set(value) != {"schema", "episodes"}:
        raise ForgeError("Dataset requires schema and episodes")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ForgeError("Learning dataset schema must be 1")
    episodes = value["episodes"]
    if not isinstance(episodes, list) or not 1 <= len(episodes) <= 10000:
        raise ForgeError("Learning dataset needs 1..10000 episodes")
    _reject_secrets(canonical(value))
    verified = []
    identities = set()
    for episode in episodes:
        if not isinstance(episode, dict) or set(episode) - EPISODE_FIELDS or not REQUIRED_FIELDS <= set(episode):
            raise ForgeError("Invalid episode fields")
        task_id = text(episode["task_id"], "episode task id", 200)
        scope = _scope(episode["scope"])
        lesson = text(episode["lesson"], "lesson", 10000)
        if episode["split"] not in ("train", "holdout"):
            raise ForgeError("Episode split must be train or holdout")
        if episode["origin"] not in ("real", "synthetic"):
            raise ForgeError("Episode origin must be real or synthetic")
        if type(episode["baseline_pass"]) is not bool or type(episode["candidate_pass"]) is not bool:
            raise ForgeError("Episode outcomes must be booleans")
        if episode["consent"] is not True:
            raise ForgeError("Learning data requires explicit consent")
        identity = (scope, task_id)
        if identity in identities:
            raise ForgeError("Duplicate or train/holdout-overlapping task id within scope")
        identities.add(identity)
        result = {**episode, "task_id": task_id, "scope": scope, "lesson": lesson,
                  "evidence": _evidence(episode["evidence"])}
        if "provenance" in episode:
            result["provenance"] = _provenance(episode["provenance"])
        verified.append(result)
    # Detect ordinary edits while the dataset/evidence was being validated.
    if _file_hash(path) != raw_hash:
        raise ForgeError("Dataset changed while validating")
    return path, raw_hash, verified


def _analyze(dataset_path, scope, output_path=None):
    scope = _scope(scope)
    path, dataset_hash, episodes = _read_dataset(dataset_path)
    if output_path is not None:
        protected = {str(path.resolve())}
        protected.update(item["path"] for episode in episodes for item in episode["evidence"])
        output = Path(output_path).absolute()
        aliases = str(output.resolve()) in protected
        if not aliases and output.exists():
            aliases = any(output.samefile(source) for source in protected)
        if aliases:
            raise ForgeError("Playbook output must not overwrite dataset or evidence")
    groups = defaultdict(list)
    for episode in episodes:
        if episode["scope"] == scope:
            groups[episode["lesson"]].append(episode)
    if not groups:
        raise ForgeError("No episodes match the requested learning scope")
    lessons = []
    for lesson, records in sorted(groups.items()):
        train_wins = [e for e in records if e["split"] == "train" and e["origin"] == "real"
                      and not e["baseline_pass"] and e["candidate_pass"]]
        holdout_wins = [e for e in records if e["split"] == "holdout" and e["origin"] == "real"
                        and not e["baseline_pass"] and e["candidate_pass"]]
        regressions = [e for e in records if e["split"] == "holdout"
                       and e["baseline_pass"] and not e["candidate_pass"]]
        if train_wins and len(holdout_wins) >= 2 and not regressions:
            status = "active"
        elif train_wins and not regressions:
            status = "candidate"
        else:
            status = "inconclusive"
        lessons.append({"text": lesson, "status": status, "support": {
            "train_real_wins": len(train_wins), "holdout_real_wins": len(holdout_wins),
            "holdout_regressions": len(regressions), "episode_count": len(records),
            "episodes": records,
        }})
    return {"schema": 1, "kind": "agent_playbook_not_model_weights", "scope": scope,
            "dataset_path": str(path.resolve()), "dataset_sha256": dataset_hash,
            "lessons": lessons, "caveats": list(CAVEATS)}


def train(dataset_path, output_path, scope):
    """Validate episodes, derive a scoped playbook and atomically persist it.

    Synthetic examples can exercise validation but cannot earn promotion.
    No inference API, arbitrary command or fine-tuning job is executed.
    """
    result = _analyze(dataset_path, scope, output_path)
    output = Path(output_path).absolute()
    atomic_json(output, result)
    return result


def active_lessons(path, scope):
    """Return active lesson strings only after recomputing their evidence gate."""
    scope = _scope(scope)
    playbook = load_json(path)
    if not isinstance(playbook, dict) or playbook.get("scope") != scope:
        raise ForgeError("Playbook scope mismatch")
    source = playbook.get("dataset_path")
    if not isinstance(source, str) or not Path(source).is_absolute():
        raise ForgeError("Playbook requires an absolute dataset path")
    expected = _analyze(source, scope)
    if canonical(playbook) != canonical(expected):
        raise ForgeError("Playbook does not match current verified dataset and evidence")
    return [lesson["text"] for lesson in expected["lessons"] if lesson["status"] == "active"]


def export_sft(dataset_path, output_path):
    """Refuse to fabricate chat traces from outcome-only episodes."""
    _read_dataset(dataset_path)
    raise ForgeError("Outcome episodes are not message traces; SFT export requires separately consented, reviewed real messages")
