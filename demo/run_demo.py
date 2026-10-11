"""The technical demo: six submissions through the real validator service.

    python demo/run_demo.py                  # settles nothing (dry run)
    python demo/run_demo.py --photo-a mine.jpg --photo-b other.jpg

It starts the validator's API on this machine, sends it six prepared
submissions over HTTP, exactly as the demo site's backend would, and prints
what came back:

    1. junk text                      -> rejected, with the reason
    2. a file that is not a photo     -> rejected, with the reason
    3. an honest submission           -> scored and paid
    4. the same photo, same person    -> not rewarded (their own re-upload)
    5. a re-saved copy, someone else  -> not rewarded (a near-copy)
    6. a second honest submission     -> scored and paid

By default nothing reaches Solana, and every answer says `dry-run`. To pay on
devnet, set the `S2E_*` variables for `S2E_CHAIN=solana` (see api/main.py)
before running, and pass two registered wallets with --wallet-a and --wallet-b.

Needs the `api` and `dev` extras (uvicorn to serve, httpx to call).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import secrets
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

VALIDATOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VALIDATOR / "src"))

from s2e_validator.settlement import base58  # noqa: E402

CASTLE = (
    "Zamek na wzgórzu nad rzeką o zachodzie słońca. Na pierwszym planie kamienny most "
    "z trzema łukami, a za nim czerwone dachy starego miasta."
)
MARKET = (
    "Stragan z warzywami na targu w sobotni poranek: skrzynki z pomidorami, pęczki "
    "rzodkiewek i koperku, a obok waga i ręcznie pisane ceny."
)
OTHER_WORDS = (
    "Widok na stary zamek z drugiego brzegu, tuż przed zmrokiem. Most prowadzi prosto "
    "do bramy, a w oknach wieży pali się światło."
)

COPIED = (
    "Piękny zamek stoi wysoko nad wodą, a do środka wchodzi się przez długi most. "
    "Zdjęcie zrobione wieczorem, kiedy niebo robi się pomarańczowe."
)
NOT_A_PHOTO = b"This is a text file renamed to photo.jpg. " * 200

#: Post one submission and return the HTTP status and the JSON answer.
Post = Callable[[str, str, bytes], tuple[int, dict]]


@dataclass(frozen=True)
class Step:
    title: str
    expect: str
    wallet: str
    description: str
    photo: bytes


def demo_wallet(name: str) -> str:
    """A well-formed address nobody holds the key to. Fine for a dry run."""
    return base58.encode(hashlib.sha256(f"s2e-demo-{name}".encode()).digest())


def placeholder_photo(scene: int) -> bytes:
    """A stand-in picture, so the demo runs without any photo on disk."""
    width, height = 1200, 900
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    top, bottom = [((40, 60, 120), (240, 150, 80)), ((90, 150, 90), (230, 220, 170))][scene % 2]
    for y in range(height):
        mix = y / height
        draw.line([(0, y), (width, y)], fill=tuple(int(a + (b - a) * mix) for a, b in zip(top, bottom)))
    for n in range(7):
        x = 90 + n * 160 + scene * 37
        tall = 180 + ((n * 97 + scene * 53) % 260)
        draw.rectangle([x, height - 200 - tall, x + 110, height - 200], fill=(60 + n * 20, 50, 70 + scene * 40))
    draw.ellipse([820 - scene * 300, 120, 980 - scene * 300, 280], fill=(250, 230, 150))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def resaved(photo: bytes) -> bytes:
    """The same picture, smaller and saved again: what a copier would upload."""
    image = Image.open(io.BytesIO(photo)).convert("RGB")
    image = image.resize((image.width * 2 // 3, image.height * 2 // 3))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=60)
    return buffer.getvalue()


def steps(photo_a: bytes, photo_b: bytes, wallet_a: str, wallet_b: str) -> list[Step]:
    return [
        Step("Junk text", "rejected", wallet_a, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", photo_a),
        Step("A file that is not a photo", "rejected", wallet_a, CASTLE, NOT_A_PHOTO),
        Step("An honest submission", "paid", wallet_a, CASTLE, photo_a),
        Step("The same photo again, same person", "not_rewarded", wallet_a, OTHER_WORDS, photo_a),
        Step("A re-saved copy of that photo, someone else", "not_rewarded", wallet_b, COPIED, resaved(photo_a)),
        Step("A second honest submission", "paid", wallet_b, MARKET, photo_b),
    ]


def describe(answer: dict) -> list[str]:
    state = answer.get("state", "?")
    lines = [f"   outcome : {state.upper().replace('_', ' ')}"]
    if answer.get("rejected_by"):
        lines.append(f"   stopped by filter : {answer['rejected_by']}")
    if answer.get("reason_code"):
        lines.append(f"   reason  : {answer['reason_code']}")
    if answer.get("quality_score") is not None:
        lines.append(f"   score   : {answer['quality_score']} / 100")
    if answer.get("reward_display") and state in ("paid", "batched"):
        lines.append(f"   reward  : {answer['reward_display']} tokens ({answer['reward_units']} base units)")
    if answer.get("transaction"):
        lines.append(f"   transaction : {answer['transaction']}")
        if answer.get("settled_on") == "devnet":
            lines.append(f"   https://explorer.solana.com/tx/{answer['transaction']}?cluster=devnet")
    if answer.get("detail"):
        lines.append(f"   detail  : {answer['detail']}")
    return lines


def play(post: Post, plan: list[Step], out: Callable[[str], None] = print, pause: float = 0.0) -> list[dict]:
    """Send every step and print the story. Returns the answers."""
    answers = []
    for number, step in enumerate(plan, 1):
        out("")
        out(f"{number}. {step.title}")
        out(f"   wallet  : {step.wallet[:4]}...{step.wallet[-4:]}")
        out(f"   text    : {step.description[:70]}{'...' if len(step.description) > 70 else ''}")
        status, answer = post(step.wallet, step.description, step.photo)
        if status != 200:
            out(f"   HTTP {status}: {answer}")
        for line in describe(answer):
            out(line)
        mark = "as expected" if answer.get("state") == step.expect else f"EXPECTED {step.expect}"
        out(f"   ({mark})")
        answers.append(answer)
        if pause:
            time.sleep(pause)
    paid = [a for a in answers if a.get("state") == "paid"]
    out("")
    out(f"{len(plan)} submissions: {len(paid)} paid, {len(plan) - len(paid)} stopped or not rewarded.")
    if answers and answers[0].get("settled_on") == "dry-run":
        out("This was a dry run: nothing was sent to Solana.")
    return answers


def _serve(port: int, verbose: bool = False):
    """Start the real service on localhost and return (server, api key)."""
    import uvicorn

    from s2e_validator.api.main import build_app

    workdir = Path(tempfile.mkdtemp(prefix="s2e-demo-"))
    os.environ.setdefault("S2E_CHAIN", "dry-run")
    os.environ.setdefault("S2E_PIPELINE_CONFIG", str(VALIDATOR / "config" / "demo_campaign.json"))
    os.environ.setdefault("S2E_BATCH_LOG", str(workdir / "batches.sqlite"))
    os.environ.setdefault("S2E_API_KEY", secrets.token_urlsafe(48))
    # The demo run can pay 100,000 base units in total (#87), so 1 unit per
    # point: a score of 75 pays 75 base units, 0.000075 of a 6-decimal token.
    os.environ.setdefault("S2E_UNITS_PER_POINT", "1")
    os.environ.setdefault("S2E_MAX_UNITS_PER_SUBMISSION", "100")
    # Quiet by default, so the recording shows the story and not the log.
    os.environ.setdefault("S2E_LOG_LEVEL", "DEBUG" if verbose else "ERROR")
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 60
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("the service did not start")
        time.sleep(0.1)
    return server, os.environ["S2E_API_KEY"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the technical demo against the real validator service.")
    parser.add_argument("--photo-a", type=Path, help="your own photo for the first honest submission")
    parser.add_argument("--photo-b", type=Path, help="a different photo for the second honest submission")
    parser.add_argument("--wallet-a", default=demo_wallet("a"), help="first contributor's address")
    parser.add_argument("--wallet-b", default=demo_wallet("b"), help="second contributor's address")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pause", type=float, default=1.5, help="seconds between steps, for a recording")
    parser.add_argument("--verbose", action="store_true", help="also show the service's own log lines")
    args = parser.parse_args(argv)

    import httpx

    photo_a = args.photo_a.read_bytes() if args.photo_a else placeholder_photo(0)
    photo_b = args.photo_b.read_bytes() if args.photo_b else placeholder_photo(1)
    server, api_key = _serve(args.port, args.verbose)
    if not args.verbose:
        import logging

        logging.getLogger("httpx").setLevel(logging.WARNING)
    base = f"http://127.0.0.1:{args.port}"
    with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {api_key}"}, timeout=200) as client:

        def post(wallet: str, description: str, photo: bytes) -> tuple[int, dict]:
            response = client.post(
                "/submissions",
                data={"wallet": wallet, "description": description},
                files={"photo": ("photo.jpg", photo, "image/jpeg")},
            )
            return response.status_code, response.json()

        health = client.get("/healthz").json()
        print(f"Survey2Earn validator, running locally. Settles on: {health['settled_on']}")
        print("Scoring: stub judge (every accepted submission scores 75).")
        try:
            play(post, steps(photo_a, photo_b, args.wallet_a, args.wallet_b), pause=args.pause)
        finally:
            server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
