import json

import httpx
import pytest

from cat.llm import BackendError, OllamaBackend, call_json, extract_json, MockBackend


def backend(cfg, handler):
    return OllamaBackend(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))


def chat_reply(content):
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


def test_chat_json_payload_and_parse(cfg):
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return chat_reply('{"action": "nap"}')
    b = backend(cfg, h)
    out = b.chat_json("decide", "qwen3:14b", [{"role": "user", "content": "hi"}], {"type": "object"})
    body = seen["body"]
    assert out == {"action": "nap"} and seen["url"].endswith("/api/chat")
    assert body["model"] == "qwen3:14b" and body["stream"] is False
    assert body["format"] == {"type": "object"} and body["think"] is False
    assert body["options"]["temperature"] == cfg.settings["llm_temperature"]["decide"]
    assert body["options"]["num_ctx"] == cfg.settings["llm_num_ctx"]


def test_sleep_tasks_use_sleep_temperature(cfg):
    seen = {}
    b = backend(cfg, lambda r: (seen.update(b=json.loads(r.content)), chat_reply("{}"))[1])
    b.chat_json("sleep_consolidate", "m", [], {})
    assert seen["b"]["options"]["temperature"] == cfg.settings["llm_temperature"]["sleep"]


@pytest.mark.parametrize("content", ["not json", "[1,2]", "", None])
def test_bad_content_is_backend_error(cfg, content):
    b = backend(cfg, lambda r: chat_reply(content))
    with pytest.raises(BackendError):
        b.chat_json("decide", "m", [], {})


def test_http_error_and_timeout_and_connect(cfg):
    with pytest.raises(BackendError, match="HTTP 500"):
        backend(cfg, lambda r: httpx.Response(500, text="boom")).chat_json("decide", "m", [], {})

    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(BackendError, match="timeout"):
        backend(cfg, timeout).chat_json("decide", "m", [], {})

    def refused(req):
        raise httpx.ConnectError("refused", request=req)
    with pytest.raises(BackendError):
        backend(cfg, refused).chat_json("decide", "m", [], {})


def tags(*names):
    return lambda r: httpx.Response(200, json={"models": [{"name": n} for n in names]})


def test_is_online(cfg):
    assert backend(cfg, tags("qwen3:14b", "qwen3:4b", "nomic-embed-text:latest")).is_online()
    assert not backend(cfg, lambda r: httpx.Response(503)).is_online()

    def refused(req):
        raise httpx.ConnectError("refused", request=req)
    assert not backend(cfg, refused).is_online()


def test_embed(cfg):
    b = backend(cfg, lambda r: httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]}))
    assert b.embed("nomic-embed-text", ["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
    bad = backend(cfg, lambda r: httpx.Response(200, json={"embeddings": [[0.1]]}))
    with pytest.raises(BackendError):
        bad.embed("m", ["a", "b"])


def test_extract_json_tolerates_think_and_fences():
    assert extract_json('<think>hmm</think>{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_call_json_retries_then_raises():
    m = MockBackend()
    m.fail_next = 1
    assert call_json(m, "decide", "m", [], {}, lambda r: r, retries=1)
    m.fail_next = 2
    with pytest.raises(BackendError):
        call_json(m, "decide", "m", [], {}, lambda r: r, retries=1)
    assert len(m.calls) == 1   # only the successful first call was recorded


def test_call_json_retries_on_unparseable_content():
    m = MockBackend()
    outs = iter([{"nope": 1}, {"outcome": "ok"}])
    m.handlers["decide"] = lambda msgs, schema: next(outs)

    def parse(raw):
        if "outcome" not in raw:
            raise BackendError("no outcome")
        return raw
    assert call_json(m, "decide", "m", [], {}, parse, retries=1) == {"outcome": "ok"}


def test_online_requires_configured_models(cfg):
    b = backend(cfg, tags("qwen3:14b", "qwen3:4b"))          # embedding model not pulled
    assert b.missing_models() == ["nomic-embed-text"] and not b.is_online()
    assert backend(cfg, tags("qwen3:14b", "qwen3:4b", "nomic-embed-text")).is_online()


def test_bare_model_name_matches_latest_tag(cfg):
    assert backend(cfg, tags("qwen3:14b", "qwen3:4b", "nomic-embed-text:latest")).missing_models() == []
    cfg.settings["nit"]["models"].update({k: "llama3.1" for k in cfg.settings["nit"]["models"] if k != "embed_model"})
    assert backend(cfg, tags("llama3.1:latest", "nomic-embed-text")).missing_models() == []
    assert backend(cfg, tags("llama3.1:8b", "nomic-embed-text")).missing_models() == ["llama3.1"]


def test_missing_models_unreachable_is_none(cfg):
    def refused(req):
        raise httpx.ConnectError("refused", request=req)
    assert backend(cfg, refused).missing_models() is None
