# -*- coding: utf-8 -*-
"""Tests for ``worker._normalize_result_payload`` and friends (Issue #73).

The worker normalizes the ``result`` and ``args`` columns before
serving them on ``/api/jobs/<id>`` so that:

1. The DB-stored JSON string is returned as a parsed object, matching
   how ``args`` was already handled.
2. kamui-code MCP's ``remote_result.content[].text`` (which is *also*
   a JSON-encoded string) is annotated with a sibling ``text_parsed``
   field carrying the parsed value. This removes the need for the
   dashboard (or any other client) to re-parse it.

These tests pin both behaviors plus the failure modes the helper has
to tolerate without raising: non-JSON ``result`` strings, missing
``content`` array, non-dict items in ``content``, oversized payloads,
and pre-parsed (already-dict) inputs.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_queue.worker import (  # noqa: E402
    _annotate_content_text,
    _normalize_args_payload,
    _normalize_result_payload,
    _try_json_loads,
)


# ---------------------------------------------------------------------------
# _try_json_loads
# ---------------------------------------------------------------------------


class TestTryJsonLoads(unittest.TestCase):
    """The low-level helper that decides whether a string looks like
    JSON and parses it without raising. Used by the result normalizer
    (and by ``_annotate_content_text``) to decide whether
    ``content[].text`` looks like an embedded JSON payload worth
    expanding into ``text_parsed``. The args normalizer intentionally
    does **not** route through this helper — see Codex re-review #6
    in PR #75 for why."""

    def test_parses_object_string(self):
        self.assertEqual(_try_json_loads('{"a": 1}'), {"a": 1})

    def test_parses_array_string(self):
        self.assertEqual(_try_json_loads('[1, 2, 3]'), [1, 2, 3])

    def test_returns_none_for_non_string(self):
        self.assertIsNone(_try_json_loads(None))
        self.assertIsNone(_try_json_loads(42))
        self.assertIsNone(_try_json_loads({"a": 1}))

    def test_returns_none_for_prose(self):
        """Bare prose must not be misinterpreted as JSON. Notably,
        plain strings, words, numbers as text, etc. should be left
        alone — even if `json.loads` would parse them."""
        self.assertIsNone(_try_json_loads("hello"))
        self.assertIsNone(_try_json_loads("42"))
        self.assertIsNone(_try_json_loads("true"))
        self.assertIsNone(_try_json_loads('"a string"'))

    def test_returns_none_for_empty(self):
        self.assertIsNone(_try_json_loads(""))
        self.assertIsNone(_try_json_loads("   "))

    def test_returns_none_for_malformed_json(self):
        self.assertIsNone(_try_json_loads("{unclosed"))
        self.assertIsNone(_try_json_loads("{ 'single-quoted': 1 }"))

    def test_tolerates_leading_whitespace(self):
        self.assertEqual(_try_json_loads("  \n {\"a\": 1}\n"), {"a": 1})

    def test_parses_large_json_without_size_cap(self):
        """There is intentionally NO upper size limit on JSON parsing.
        MCP services that stream large responses or return inline
        base64 (video, image batches) can legitimately deliver
        multi-MB ``result`` payloads, and silently dropping back to a
        string at this layer would break the lazy-v2.13 contract that
        ``result`` is structured. This test pins that a several-MB
        valid JSON object parses successfully."""
        # Build a ~5 MB valid JSON object with a long string value.
        large_value = "x" * 5_000_000
        payload = '{"data": "' + large_value + '"}'
        result = _try_json_loads(payload)
        self.assertIsInstance(result, dict)
        self.assertEqual(result, {"data": large_value})


# ---------------------------------------------------------------------------
# _annotate_content_text
# ---------------------------------------------------------------------------


class TestAnnotateContentText(unittest.TestCase):
    """Single-level expansion of ``content[].text`` into ``text_parsed``,
    operating in place on the list."""

    def test_annotates_json_text(self):
        content = [
            {"type": "text", "text": '{"images": [{"url": "https://x/y.png"}]}'},
        ]
        _annotate_content_text(content)
        self.assertEqual(
            content[0]["text_parsed"],
            {"images": [{"url": "https://x/y.png"}]},
        )
        # Original `text` is preserved untouched.
        self.assertEqual(
            content[0]["text"],
            '{"images": [{"url": "https://x/y.png"}]}',
        )

    def test_skips_non_json_text(self):
        content = [{"type": "text", "text": "just a status message"}]
        _annotate_content_text(content)
        self.assertNotIn("text_parsed", content[0])

    def test_skips_non_dict_items(self):
        """A defensive guard for ``content`` arrays that contain
        unexpected primitives. We must not raise."""
        content = ["a string", 42, None, {"text": '{"x": 1}'}]
        _annotate_content_text(content)
        self.assertEqual(content[3]["text_parsed"], {"x": 1})
        # The non-dict entries are still in place, unmodified.
        self.assertEqual(content[0], "a string")
        self.assertEqual(content[1], 42)
        self.assertIsNone(content[2])

    def test_skips_missing_text_field(self):
        content = [{"type": "image"}]  # no `text` at all
        _annotate_content_text(content)
        self.assertNotIn("text_parsed", content[0])

    def test_skips_non_string_text(self):
        content = [{"type": "text", "text": 42}]
        _annotate_content_text(content)
        self.assertNotIn("text_parsed", content[0])

    def test_non_list_input_is_noop(self):
        # Just must not raise.
        _annotate_content_text(None)  # type: ignore[arg-type]
        _annotate_content_text("not a list")  # type: ignore[arg-type]
        _annotate_content_text({"not": "a list"})  # type: ignore[arg-type]

    def test_only_single_level_expansion(self):
        """If ``text_parsed`` itself happens to contain another
        ``content[].text`` shape, we do NOT recurse. Bounded
        normalization keeps the helper predictable."""
        inner_text = '{"images": [{"url": "https://x/y.png"}]}'
        outer_text = (
            '{"remote_result": {"content": [{"type": "text", "text": '
            + json.dumps(inner_text) + "}]}}"
        )
        content = [{"type": "text", "text": outer_text}]
        _annotate_content_text(content)
        # Outer level is parsed.
        outer_parsed = content[0]["text_parsed"]
        self.assertIsInstance(outer_parsed, dict)
        # Inner `content[].text` is NOT auto-expanded here; only the
        # full normalizer (`_normalize_result_payload`) walks one
        # remote_result.content layer below the top level.
        inner_content = outer_parsed["remote_result"]["content"]
        self.assertNotIn("text_parsed", inner_content[0])


# ---------------------------------------------------------------------------
# _normalize_result_payload
# ---------------------------------------------------------------------------


class TestNormalizeResultPayload(unittest.TestCase):
    """The end-to-end result normalizer used by the /api/jobs/<id>
    endpoint."""

    def test_parses_json_string_into_object(self):
        raw = '{"images": [{"url": "https://x/y.png"}]}'
        out = _normalize_result_payload(raw)
        self.assertEqual(out, {"images": [{"url": "https://x/y.png"}]})

    def test_kamui_double_json_gets_text_parsed_annotation(self):
        """Real kamui-code shape from production: the outer `result`
        is JSON-string, and inside it `remote_result.content[].text`
        is another JSON-encoded string carrying the actual URLs."""
        inner = {"images": [{"url": "https://example.com/files/a.png"}]}
        outer = {
            "remote_result": {
                "content": [
                    {"type": "text", "text": json.dumps(inner)},
                ],
            },
            "local_files": ["C:\\Users\\u\\out.png"],
            "download_errors": [],
        }
        raw = json.dumps(outer)
        out = _normalize_result_payload(raw)

        # Outer is a dict now
        self.assertIsInstance(out, dict)
        # Annotation present
        item = out["remote_result"]["content"][0]
        self.assertEqual(item["text_parsed"], inner)
        # Original text is preserved
        self.assertEqual(item["text"], json.dumps(inner))
        # local_files survives the round-trip unchanged
        self.assertEqual(out["local_files"], ["C:\\Users\\u\\out.png"])

    def test_kamui_double_json_video_url(self):
        """Same as above but with the video.url shape (t2v / r2v
        completed jobs)."""
        inner = {"video": {"url": "https://example.com/files/v.mp4"},
                 "seed": 12345}
        outer = {
            "remote_result": {
                "content": [{"type": "text", "text": json.dumps(inner)}],
            },
        }
        out = _normalize_result_payload(json.dumps(outer))
        item = out["remote_result"]["content"][0]
        self.assertEqual(item["text_parsed"], inner)

    def test_invalid_json_returned_as_string(self):
        """An executor that stored unstructured text (e.g. an HTML
        error page) must not break the response. The raw string is
        returned and the client decides how to render it."""
        raw = "<html><body>500 Internal Server Error</body></html>"
        out = _normalize_result_payload(raw)
        self.assertEqual(out, raw)

    def test_none_passes_through(self):
        self.assertIsNone(_normalize_result_payload(None))

    def test_empty_string_passes_through(self):
        self.assertEqual(_normalize_result_payload(""), "")

    def test_already_parsed_dict_gets_annotated(self):
        """If a caller pre-parses (or a future code path stores the
        result as JSON in the DB), we still annotate
        ``remote_result.content[].text``."""
        inner = {"images": [{"url": "https://x/a.png"}]}
        pre_parsed = {
            "remote_result": {
                "content": [{"type": "text", "text": json.dumps(inner)}],
            },
        }
        out = _normalize_result_payload(pre_parsed)
        self.assertEqual(
            out["remote_result"]["content"][0]["text_parsed"], inner,
        )

    def test_missing_remote_result_is_fine(self):
        """Results without the kamui-code shape (e.g. minimal
        executor results) must pass through unmodified."""
        raw = json.dumps({"local_files": ["C:\\out.png"], "ok": True})
        out = _normalize_result_payload(raw)
        self.assertEqual(out, {"local_files": ["C:\\out.png"], "ok": True})

    def test_remote_result_without_content_is_fine(self):
        raw = json.dumps({"remote_result": {"status": "completed"}})
        out = _normalize_result_payload(raw)
        # Untouched (no `content` array → no annotation work)
        self.assertEqual(out, {"remote_result": {"status": "completed"}})

    def test_content_text_that_is_not_json(self):
        """An MCP server that returned plain prose in
        ``content[].text`` should not get a ``text_parsed`` field."""
        raw = json.dumps({
            "remote_result": {
                "content": [{"type": "text", "text": "Generation complete."}],
            },
        })
        out = _normalize_result_payload(raw)
        item = out["remote_result"]["content"][0]
        self.assertEqual(item["text"], "Generation complete.")
        self.assertNotIn("text_parsed", item)


# ---------------------------------------------------------------------------
# _normalize_args_payload
# ---------------------------------------------------------------------------


class TestNormalizeArgsPayload(unittest.TestCase):
    """Mirror of the result helper for the args column. Notably it
    does NOT recurse into ``content[].text`` — submitted args have
    never used that shape and we do not want surprise expansion of
    prompts that happen to look like JSON."""

    def test_parses_json_object_string(self):
        raw = '{"prompt": "a cat", "size": "1024x1024"}'
        out = _normalize_args_payload(raw)
        self.assertEqual(out, {"prompt": "a cat", "size": "1024x1024"})

    def test_invalid_json_returned_as_string(self):
        raw = "this is not JSON"
        self.assertEqual(_normalize_args_payload(raw), raw)

    def test_none_passes_through(self):
        self.assertIsNone(_normalize_args_payload(None))

    def test_already_parsed_dict_returned_unchanged(self):
        d = {"a": 1, "b": 2}
        self.assertEqual(_normalize_args_payload(d), d)

    def test_parses_scalar_json_for_legacy_compat(self):
        """Legacy `json.loads(j["args"])` accepted scalar JSON values
        (`"42"`, `"true"`, `"null"`, `"\\"x\\""`). The normalizer must
        preserve that behavior — routing through a conservative
        "looks like object/array" check would silently change the
        return type for any consumer that submitted a bare JSON
        scalar as args. Codex re-review #6."""
        # Number scalar
        self.assertEqual(_normalize_args_payload("42"), 42)
        # Boolean scalar
        self.assertEqual(_normalize_args_payload("true"), True)
        # null scalar → Python None (distinct from "args was empty
        # string", which still falls through as ""—see test_none_passes_through)
        self.assertIsNone(_normalize_args_payload("null"))
        # String scalar (JSON-encoded string → Python str)
        self.assertEqual(_normalize_args_payload('"hello"'), "hello")
        # Float
        self.assertEqual(_normalize_args_payload("3.14"), 3.14)

    def test_does_not_recurse_into_content_text(self):
        """Even if (hypothetically) submitted args carried a
        ``remote_result.content[].text`` shape with JSON-as-string,
        the args normalizer must not expand it — args expansion would
        change semantics for clients that compare the round-tripped
        args against the submitted ones byte-for-byte."""
        raw = json.dumps({
            "remote_result": {
                "content": [{"type": "text", "text": '{"x": 1}'}],
            },
        })
        out = _normalize_args_payload(raw)
        # Top-level parsed (consistent with the legacy behavior of
        # `json.loads(job["args"])` in worker.py).
        self.assertIsInstance(out, dict)
        # But the embedded JSON-as-string stays a string.
        self.assertEqual(
            out["remote_result"]["content"][0]["text"], '{"x": 1}',
        )
        self.assertNotIn("text_parsed", out["remote_result"]["content"][0])


if __name__ == "__main__":
    unittest.main()
