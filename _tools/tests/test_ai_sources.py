"""Tests for ai-monitor/spend.py and localllm.py parsers (documented payload shapes)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-monitor"))

import localllm  # noqa: E402
import spend  # noqa: E402

ANTHROPIC_COST = {"data": [
    {"starting_at": "2026-10-15T00:00:00Z", "ending_at": "2026-10-16T00:00:00Z",
     "results": [{"amount": "12345.0", "currency": "USD"}, {"amount": "55.5", "currency": "USD"}]},
    {"starting_at": "2026-10-16T00:00:00Z", "ending_at": "2026-10-17T00:00:00Z",
     "results": [{"amount": "250", "currency": "USD"}]},
    {"starting_at": "2026-10-17T00:00:00Z", "ending_at": "2026-10-18T00:00:00Z", "results": []},
], "has_more": False, "next_page": None}
OPENAI_COST = {"object": "page", "data": [
    {"object": "bucket", "start_time": 1792022400, "end_time": 1792108800,
     "results": [{"object": "organization.costs.result", "amount": {"value": 1.25, "currency": "usd"}}]},
    {"object": "bucket", "start_time": 1792108800, "end_time": 1792195200,
     "results": [{"amount": {"value": 0.5, "currency": "usd"}}, {"amount": {"value": 0.25, "currency": "usd"}}]},
]}
OPENROUTER_KEY = {"data": {"label": "k", "usage": 40.0, "usage_daily": 1.5, "usage_monthly": 12.25,
                           "limit": 50.0, "limit_remaining": 10.0}}
VLLM_METRICS = """# HELP vllm:num_requests_running Number of requests running
vllm:num_requests_running{model_name="qwen"} 3.0
vllm:num_requests_waiting{model_name="qwen"} 1.0
vllm:generation_tokens_total{model_name="qwen"} 1000.0
vllm:gpu_cache_usage_perc{model_name="qwen"} 0.42
"""
LLAMACPP_METRICS = """llamacpp:requests_processing 1
llamacpp:requests_deferred 0
llamacpp:predicted_tokens_seconds 38.5
llamacpp:tokens_predicted_total 5000
"""


class SpendTest(unittest.TestCase):
    def test_anthropic_cents_strings(self):
        s = spend.parse_anthropic(ANTHROPIC_COST, today="2026-10-16")
        self.assertAlmostEqual(s.month_usd, (12345.0 + 55.5 + 250) / 100)
        self.assertAlmostEqual(s.today_usd, 2.50)

    def test_openai_dollar_floats(self):
        s = spend.parse_openai(OPENAI_COST, today_start=1792108800)
        self.assertAlmostEqual(s.month_usd, 2.0)
        self.assertAlmostEqual(s.today_usd, 0.75)

    def test_openrouter_key(self):
        s = spend.parse_openrouter(OPENROUTER_KEY)
        self.assertEqual((s.today_usd, s.month_usd), (1.5, 12.25))
        self.assertIn("10", s.detail)

    def test_garbage_raises_parse_error(self):
        for fn, args in ((spend.parse_anthropic, ({"data": "x"}, "2026-10-16")),
                         (spend.parse_openai, ([], 0)), (spend.parse_openrouter, ({},))):
            with self.assertRaises(spend.SourceError):
                fn(*args)

    def test_money_format(self):
        self.assertEqual(spend.money(0), "$0")
        self.assertEqual(spend.money(1.234), "$1.23")
        self.assertEqual(spend.money(1234.5), "$1.2k")


class LocalLlmTest(unittest.TestCase):
    def test_ollama_ps(self):
        ps = {"models": [{"name": "qwen3:8b", "size": 6_000_000_000, "size_vram": 5_500_000_000,
                          "expires_at": "2026-10-16T14:38:31.83753-07:00"}]}
        s = localllm.parse_ollama(ps, {"version": "0.12.3"})
        self.assertEqual(s.summary, "1 loaded")
        self.assertIn("qwen3:8b", s.rows[0][0])
        self.assertIn("5.5G", s.rows[0][1])

    def test_lmstudio_loaded_state(self):
        data = {"data": [{"id": "llama-3.1-8b", "state": "loaded", "type": "llm"},
                         {"id": "nomic-embed", "state": "not-loaded", "type": "embeddings"}]}
        s = localllm.parse_lmstudio(data)
        self.assertEqual(s.summary, "1 loaded")
        self.assertEqual(len(s.rows), 1)

    def test_prometheus_parse_sums_labels(self):
        m = localllm.parse_prometheus('a{x="1"} 1\na{x="2"} 2\n# c\nb 3.5\nbad line\n')
        self.assertEqual((m["a"], m["b"]), (3.0, 3.5))

    def test_vllm_rate_from_counter_and_old_kv_name(self):
        rate = localllm.RateTracker()
        first = localllm.parse_vllm(localllm.parse_prometheus(VLLM_METRICS), rate, now=0)
        later = VLLM_METRICS.replace("1000.0", "1600.0")
        second = localllm.parse_vllm(localllm.parse_prometheus(later), rate, now=10)
        self.assertEqual(first.summary, "3 running")
        self.assertIn("60 tok/s", second.detail)
        self.assertIn(("KV cache", "42%"), second.rows)

    def test_llamacpp(self):
        s = localllm.parse_llamacpp(localllm.parse_prometheus(LLAMACPP_METRICS), {"status": "ok"})
        self.assertEqual(s.summary, "1 busy")
        self.assertIn("38", s.detail)


if __name__ == "__main__":
    unittest.main()
