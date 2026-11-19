import pytest

from cat.llm import BackendError, MockBackend, cosine, hash_embed


def test_mock_outputs_for_all_tasks():
    m = MockBackend(seed=1)
    for t in ("brain", "narrator", "consolidate", "tendencies", "preferences"):
        assert isinstance(m.chat_json(t, "m", [], {}), dict)


def test_mock_deterministic():
    a = [MockBackend(seed=3).chat_json("brain", "m", [], {}) for _ in range(2)]
    assert a[0] == a[1]


def test_mock_offline_and_injected_failures():
    m = MockBackend()
    m.fail_next = 1
    with pytest.raises(BackendError):
        m.chat_json("brain", "m", [], {})
    assert m.chat_json("brain", "m", [], {})
    m.online = False
    assert not m.is_online()
    with pytest.raises(BackendError):
        m.embed("e", ["x"])


def test_handler_override():
    m = MockBackend()
    m.handlers["brain"] = lambda msgs, schema: {"action": "scripted"}
    assert m.chat_json("brain", "m", [], {}) == {"action": "scripted"}


def test_hash_embed_similarity():
    a, b, c = (hash_embed(t) for t in ("climb the old tree", "climb the tree", "eat fish bowl"))
    assert cosine(a, b) > cosine(a, c)
