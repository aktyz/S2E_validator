"""The Solana client's RPC layer: plain JSON over HTTPS, and errors in words."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

pytest.importorskip("solders", reason="the Solana client needs the optional `solana` extra")

from s2e_validator.settlement import solana as s  # noqa: E402


class TestDescribeError:
    @pytest.mark.parametrize(
        "err, text",
        [
            ({"InstructionError": [1, {"Custom": 11_000}]}, "InvalidBatchSequence (program error 11000)"),
            ({"InstructionError": [1, {"Custom": 3012}]}, "AccountNotInitialized (program error 3012)"),
            ({"InstructionError": [1, {"Custom": 424_242}]}, "program error 424242"),
            ({"InstructionError": [0, "ComputationalBudgetExceeded"]}, "ComputationalBudgetExceeded"),
            ("BlockhashNotFound", "BlockhashNotFound"),
            ({"InsufficientFundsForRent": {"account_index": 0}}, '{"InsufficientFundsForRent": {"account_index": 0}}'),
        ],
    )
    def test_errors_read_as_words(self, err, text):
        assert s.describe_error(err) == text


class TestJsonRpc:
    def reply(self, payload: dict):
        def opener(request, timeout):
            self.request = request
            return io.BytesIO(json.dumps(payload).encode())

        return opener

    def test_a_call_posts_json_and_returns_the_result(self):
        rpc = s.JsonRpc("https://rpc.example/key-123", opener=self.reply({"result": 42}))

        assert rpc.call("getBlockHeight", [{"commitment": "confirmed"}]) == 42
        assert json.loads(self.request.data) == {
            "jsonrpc": "2.0", "id": 1, "method": "getBlockHeight", "params": [{"commitment": "confirmed"}],
        }

    def test_an_error_reply_carries_its_code_and_data(self):
        rpc = s.JsonRpc(
            "https://rpc.example",
            opener=self.reply({"error": {"code": -32002, "message": "simulation failed", "data": {"err": "x"}}}),
        )

        with pytest.raises(s.RpcError, match="simulation failed") as caught:
            rpc.call("sendTransaction", ["AA=="])

        assert (caught.value.code, caught.value.data) == (-32002, {"err": "x"})

    def test_a_transport_error_never_shows_the_url(self):
        """Hosted RPC URLs carry an API key."""

        def opener(request, timeout):
            raise urllib.error.URLError("cannot reach https://rpc.example/key-123")

        rpc = s.JsonRpc("https://rpc.example/key-123", opener=opener)

        with pytest.raises(s.RpcError) as caught:
            rpc.call("getBlockHeight")

        assert "key-123" not in str(caught.value)
        assert caught.value.__suppress_context__

    @pytest.mark.parametrize("url", ["http://rpc.example", "ftp://rpc.example", "rpc.example"])
    def test_a_plain_text_connection_to_a_remote_node_is_refused(self, url):
        with pytest.raises(ValueError, match="must be https"):
            s.JsonRpc(url)

    @pytest.mark.parametrize("url", ["http://localhost:8899", "http://127.0.0.1:8899", "https://api.devnet.solana.com"])
    def test_https_and_a_local_validator_are_accepted(self, url):
        s.JsonRpc(url)
