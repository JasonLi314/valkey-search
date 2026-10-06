import gzip
import os
import pickle

from .sources import compute_sources_hash

_COMPAT_DIR = os.path.dirname(os.path.abspath(__file__))


# Registry of compatibility generators. To add a new generator, create the
# generate file (subclassing BaseCompatibilityTest with its own
# ANSWER_FILE_NAME) and add an entry here. regenerate.sh and
# compatibility_test.py both read from this list. New generators go in a
# per-command subdirectory (e.g. search/); all paths are relative to this
# directory and ANSWER_FILE_NAME must match the "answers" entry.
GENERATORS = [
    {"generator": "generate.py",         "answers": "aggregate-answers.pickle.gz",    "cluster": True},
    {"generator": "generate_search.py",  "answers": "search-answers.pickle.gz",       "cluster": True},
    {"generator": "generate_text.py",    "answers": "text-search-answers.pickle.gz",  "cluster": False},
    {"generator": "generate_array.py",   "answers": "array-input-answers.pickle.gz",  "cluster": False},
    {"generator": "generate_expr.py",    "answers": "expr-answers.pickle.gz",         "cluster": False},
    {"generator": "generate_sortkey.py", "answers": "sortkey-answers.pickle.gz",      "cluster": False},
    {"generator": "generate_filter.py",  "answers": "filter-answers.pickle.gz",       "cluster": False},
    {"generator": "generate_return.py",  "answers": "return-answers.pickle.gz",       "cluster": False},
    # "cluster": False for the reason generate_text.py is -- text scores are
    # computed from shard-local corpus statistics, so these standalone-captured
    # answers cannot match a cluster replay. See unsupported_tests.md 5.9. The
    # two mechanical blockers are fixed, so this is a one-word change once
    # distributed text scoring lands.
    {"generator": "generate_hybrid.py",  "answers": "hybrid-answers.pickle.gz",       "cluster": False},
]


def stale_generators():
    """Generators whose pickle is missing or was built from different sources."""
    stale = []
    for g in GENERATORS:
        path = os.path.join(_COMPAT_DIR, g["answers"])
        stored = None
        if os.path.exists(path):
            with gzip.open(path, "rb") as f:
                payload = pickle.load(f)
            if isinstance(payload, dict):
                stored = payload.get("sources_hash")
        if stored != compute_sources_hash(g["generator"]):
            stale.append(g["generator"])
    return stale
