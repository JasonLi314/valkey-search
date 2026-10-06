import pytest, traceback, time, inspect
import random
import sys, os
import pickle
import gzip
from .data_sets import ClientSystem, load_data
from .sources import compute_sources_hash
from valkey.exceptions import ConnectionError
'''
Capture answer from Redisearch
'''
TEST_MARKER = "*" * 100

encoder = lambda x: x.encode() if not isinstance(x, bytes) else x

# format_stack() output depends only on each frame's code object and line, and
# most answers share a stack, so cache the text per frame chain.
_stack_text_cache = {}
def format_stack_cached():
    key, f = [], sys._getframe(1)
    while f is not None:
        key.append((f.f_code, f.f_lasti, f.f_lineno))
        f = f.f_back
    key = tuple(key)
    text = _stack_text_cache.get(key)
    if text is None:
        text = _stack_text_cache[key] = "".join(
            traceback.format_stack(sys._getframe(1)))
    return text

# Every generator used to run a container literally named "Generate-search"
# on a fixed port 6380, so two checkouts generating at once on one machine
# shared both: the second `docker run` replaced the first one's server and the
# first run collapsed mid-generation. 6380 also belongs to
# testing/integration/vector_search_integration_test.py, so the clash was not
# only between generators.
#
# The name now carries a per-run suffix, and the port is left to docker --
# publishing to port 0 has the kernel hand out one that is free, which a
# randomly chosen number cannot promise.
CONTAINER_PREFIX = "Generate-search"

# FT.SEARCH option keywords that terminate the free-text query expression.
# Everything between the index name and the first of these tokens is the query,
# which may itself contain whitespace (e.g. "@n1:[0 +inf]" or a VECTOR_RANGE
# clause). The compatibility generators write commands as one whitespace-joined
# string for readability, so the query span has to be rejoined into a single
# argv element before the command is issued -- otherwise a multi-token filter
# is shredded into separate arguments and the reference engine rejects it.
_FT_SEARCH_OPTION_KEYWORDS = frozenset({
    "NOCONTENT", "VERBATIM", "NOSTOPWORDS", "WITHSCORES", "WITHPAYLOADS",
    "WITHSORTKEYS", "FILTER", "GEOFILTER", "INKEYS", "INFIELDS", "RETURN",
    "SUMMARIZE", "HIGHLIGHT", "SLOP", "TIMEOUT", "INORDER", "LANGUAGE",
    "EXPANDER", "SCORER", "EXPLAINSCORE", "PAYLOAD", "SORTBY", "LIMIT",
    "PARAMS", "DIALECT",
})


def join_search_query(cmd):
    """Rejoin an FT.SEARCH query expression into a single argv token.

    `cmd` is the whitespace-split argv of an `ft.search <index> <query...>
    [OPTIONS...]` command in which the query expression may span several
    tokens. Returns a new argv where those query tokens are a single element,
    leaving the command keyword, index name, and trailing options untouched.
    Commands that are not FT.SEARCH (or have no query span) are returned as-is.
    """
    if len(cmd) < 3 or cmd[0].lower() != "ft.search":
        return cmd
    end = len(cmd)
    for i in range(2, len(cmd)):
        if cmd[i].upper() in _FT_SEARCH_OPTION_KEYWORDS:
            end = i
            break
    query = " ".join(cmd[2:end])
    return [cmd[0], cmd[1], query, *cmd[end:]]

class ClientRSystem(ClientSystem):
    def __init__(self, address):
        super().__init__(address)
        try:
            self.client.execute_command("FT.CONFIG SET TIMEOUT 0")
        except:
            pass

    def wait_for_indexing_done(self, index_name):
        '''Wait for indexing to be done.
        indexing = True
        while indexing:
            try:
                indexing = self.ft_info(index_name)["indexing"]
            except redis.ConnectionError:
                print("failed")
                assert False
                '''
        print("Indexing is done.")

class BaseCompatibilityTest:
    """Base class for compatibility tests with shared infrastructure."""
    
    # Subclasses must define this
    ANSWER_FILE_NAME = None

    # Reference engine image. Subclasses override it when they need a command
    # the default image does not implement -- FT.HYBRID, for instance, only
    # exists in the Redis 8.4+ query engine. The container name carries the
    # class name and a random suffix, so two generators never collide on it.
    
    @classmethod
    def setup_class(cls):
        if cls.ANSWER_FILE_NAME is None:
            raise NotImplementedError("Subclass must define ANSWER_FILE_NAME")
            
        cls.container_name = f"{CONTAINER_PREFIX}-{cls.__name__}-{random.randint(1000, 9999)}"
        # --search-workers 0 makes every write search-visible before the next
        # command, so no sleep is needed between loading data and querying it:
        # https://redis.io/docs/latest/develop/ai/redisvl/concepts/search-and-indexing/#search-visibility-after-writes
        if os.system(f"docker run --rm -d --name {cls.container_name} "
                     f"-p 0:6379 redis:latest "
                     f"redis-server --search-workers 0") != 0:
            print("Failed to start Redis server, please check your Docker setup.")
            sys.exit(1)
        port = cls._published_port()
        if port is None:
            os.system(f"docker stop {cls.container_name}")
            print(f"Could not read the published port of {cls.container_name}.")
            sys.exit(1)
        cls.address = ("localhost", port)
        print(f"Started {cls.container_name} server on port {port}")
        # teardown_class has no route to the pytest session, and it must not
        # write an answer file from a run that did not finish. Set it up front
        # so that a run which never reaches a test is treated as incomplete.
        cls.session = None
        cls.answers = []
        # add reply count to check redis non-empty answer
        cls.replied_count = 0
        cls.client = ClientRSystem(cls.address)
        while True:
            try:
                cls.client.execute_command("PING")
                break
            except ConnectionError:
                print("Waiting for R system to be ready...")
                time.sleep(.25)
        print("Done initializing")

    @classmethod
    def _published_port(cls):
        """The host port docker chose for the container's 6379.

        `docker port` prints one line per binding -- the IPv4 and IPv6 forms
        name the same port. The mapping exists as soon as the container is
        created, but the daemon can take a moment to report it, so this
        retries rather than losing a run to that race.
        """
        for _ in range(40):
            out = os.popen(f"docker port {cls.container_name} 6379").read().strip()
            if out:
                return int(out.splitlines()[0].rsplit(":", 1)[1])
            time.sleep(.25)
        return None

    @pytest.fixture(autouse=True)
    def _remember_session(self, request):
        type(self).session = request.session

    @classmethod
    def teardown_class(cls):
        print(f"Stopping {cls.container_name} server")
        os.system(f"docker stop {cls.container_name}")

        # A generator that died part way collected only some of its answers.
        # Writing them replaces a complete answer file with a short one that
        # still satisfies the sources-hash check, so the loss stays invisible
        # until someone counts the answers -- a flaky docker start once cut
        # text-search from 42612 answers to 11000 this way. Leave the file
        # alone; pytest's non-zero exit stops regenerate.sh.
        if cls.session is None:
            print(f"NOT writing {cls.ANSWER_FILE_NAME}: no test reported in, "
                  f"so the run never started properly.")
            return
        if cls.session.testsfailed:
            print(f"NOT writing {cls.ANSWER_FILE_NAME}: "
                  f"{cls.session.testsfailed} test(s) failed, so the "
                  f"{len(cls.answers)} answers collected are incomplete.")
            return

        # A run that was narrowed to a subset is short for the same reason a
        # failed one is, and just as quietly: `pytest generate_array.py -k
        # test_filter_missing_field` took that answer file from 490 answers to
        # 8. Only a whole, unfiltered, unaborted run may write.
        option = cls.session.config.option
        narrowed = [
            flag
            for flag, value in (
                ("-k", getattr(option, "keyword", "")),
                ("-m", getattr(option, "markexpr", "")),
                ("--deselect", getattr(option, "deselect", None)),
                ("--last-failed", getattr(option, "last_failed", False)),
            )
            if value
        ]
        if any("::" in arg for arg in cls.session.config.args):
            narrowed.append("a test id")
        if cls.session.shouldstop:
            narrowed.append("an early exit")
        if narrowed:
            print(f"NOT writing {cls.ANSWER_FILE_NAME}: {', '.join(narrowed)} "
                  f"narrowed the run, so the {len(cls.answers)} answers "
                  f"collected are incomplete.")
            return

        print("Dumping ", len(cls.answers), " answers")
        generator = os.path.relpath(inspect.getfile(cls), os.path.dirname(__file__))
        payload = {
            "sources_hash": compute_sources_hash(generator),
            "answers": cls.answers,
        }
        with gzip.open(cls.ANSWER_FILE_NAME, "wb") as answer_file:
            pickle.dump(payload, answer_file)

    def setup_method(self):
        self.client.execute_command("FLUSHALL SYNC")

    def setup_data(self, data_set_name, key_type, vector_data_type="FLOAT32"):
        self.data_set_name = data_set_name
        self.key_type = key_type
        self.vector_data_type = vector_data_type
        return load_data(self.client, data_set_name, key_type, vector_data_type=vector_data_type)

    def execute_command(self, cmd, excluded=False, excluded_cluster_only=False):
        answer = {"cmd": cmd,
                  "key_type": self.key_type,
                  "data_set_name": self.data_set_name,
                  "vector_data_type": getattr(self, "vector_data_type", "FLOAT32"),
                  "testname": os.environ.get('PYTEST_CURRENT_TEST').split(':')[-1].split(' ')[0],
                  "traceback": format_stack_cached()}
        if excluded:
            # Known, intentional difference from Redisearch. The answer is still
            # captured, but the replay only checks that valkey-search does not
            # crash on the command rather than comparing results.
            answer["excluded"] = True
        if excluded_cluster_only:
            # Difference that only exists in cluster (CME): single-node (CMD)
            # still asserts full equality against Redisearch, while the cluster
            # replay does a no-crash check only. Used for BM-25 text scoring,
            # which is shard-local in cluster by design.
            answer["excluded_cluster"] = True
        try:
            print("Cmd:", *cmd)
            answer["result"] = self.client.execute_command(*cmd)
            answer["exception"] = False
            if answer["result"] != [0]:
                self.__class__.replied_count += 1  # ADD THIS LINE
            print(f"replied: {answer['result']} (count: {self.__class__.replied_count})")
            # print(f"replied: {answer['result']}")
        except Exception as exc:
            print(f"Got exception for Error: '{exc}', Cmd:{cmd}")
            answer["result"] = {}
            answer["exception"] = True
        self.answers.append(answer)

    def check(self, *orig_cmd):
        """Non-vector command."""
        cmd = orig_cmd[0].split() if len(orig_cmd) == 1 else [*orig_cmd]
        self.execute_command(cmd)

