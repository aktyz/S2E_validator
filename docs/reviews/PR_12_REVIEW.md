<!-- review-state
pr: 12
round: 1
reviewed_head: 8327445
base: origin/main
verdict: Request Changes
last_finding_id: F-14
updated: 2026-10-30
-->

# Round 1 — Initial review · reviewed 8327445 · 2026-10-30

## PR Review: README for the validator as its own repository, with the measured two-judge setup

## Pull request overview

The validator has moved out of the Survey2Earn monorepo, and this PR gets it ready to be a public repository and a Nebius x NVIDIA Global AI Hackathon submission. The README now explains what Survey2Earn is, that `#13`-style numbers refer to the original repository, and how the judge was measured: 268 hand-labelled cases, a Qwen2.5-VL-7B judge and an NVIDIA Nemotron Nano 12B v2 VL judge, both served with vLLM on one Nebius AI Cloud L40S and combined so a submission is paid only if both agree. It also fixes statements that were no longer true (`cd validator`, the pay line of 40, "no model client yet"), adds an Apache 2.0 licence, and adds `deploy/judge-gpu.sh`, which serves both models on 127.0.0.1 from a vLLM image pinned by digest.

### Changes:
1. Repository identity and licensing:
   - Apache 2.0 `LICENSE` and a licence badge.
   - A paragraph on Survey2Earn, and one on where this repository came from and what its issue numbers refer to.
2. "The judge, measured" (new README section):
   - The labelled set, the two models, and the GPU and cloud they run on.
   - A two-judge comparison table (each model alone, combined offline, live pair) and its honest limits.
   - The history of the Qwen judge across four fixes, with what each run found.
   - "Nebius and NVIDIA in this project", including why Token Factory is not used (D5).
   - Reproduction commands for the GPU server, the measurement, and `--combine`.
3. Corrections to existing README text:
   - The Stage 2 diagram line, the status table (J2–J4, #4), the pay line now 30 (`rubric-v1.1`), the model client paragraph, the file table, and "copy this repository" instead of `validator/`.
   - Em dashes replaced with colons or removed throughout.
4. Deployment:
   - `deploy/judge-gpu.sh`: installs Docker and the NVIDIA container toolkit if missing, then starts Qwen (BF16) and Nemotron (FP8, eager mode) one after the other, each at 0.47 of GPU memory, bound to 127.0.0.1.

### Impact
- An outside reader, or a hackathon judge, can see what the validator does, how far its judge can be trusted, and how to reproduce the numbers.
- The privacy position (no contributor photo goes to a hosted API, Nebius's included) is stated, and I checked it against `judge/chat_client.py`: the refusal list includes `api.studio.nebius.ai`, `api.studio.nebius.com` and `api.tokenfactory.nebius.com`.
- No runtime code changes. The service still defaults to `S2E_JUDGE=stub`, and the live demo still runs the stub.

---

### Review Baseline
| Item            | Key Versions | Docs loaded |
|-----------------|--------------|-------------|
| PR description  | found | — |
| Validator       | Python ≥3.11; no dependency, extra or cap touched (`pyproject.toml` read, unchanged by this PR); pytest ≥8 | None needed for Python: the diff has no Python changes. |
| Hackathon rules | Nebius x NVIDIA Global AI Hackathon, submissions close 2026-10-30 10:00 PT | `https://nebiusglobalaihackathon.devpost.com/rules` returned HTTP 403 to a direct fetch, so the rule text below comes from a web search result for that page plus third-party summaries. Confirm the exact wording on the live page. |
| vLLM flags in `deploy/judge-gpu.sh` | vLLM image pinned by digest (the script says 0.32.0) | Not checked against vLLM docs. The README says the measurements ran on that exact image, which is stronger evidence that the flags work than the docs would be. |

Source files read to check README claims: `src/s2e_validator/judge/unanimous.py`, `src/s2e_validator/judge/chat_client.py`, `src/s2e_validator/api/main.py`, `calibration/measure_judge.py`, `config/demo_campaign.json`. The documented environment variables, the `--judge`, `--campaign`, `--out` and `--combine` flags, `JUDGES_DISAGREE`, provenance that names both models, `grader_patterns`, and the host refusals all match the code.

Hackathon rules as found:
- The project has to **run on Nebius Token Factory or Nebius AI Cloud**: a runtime call to the Token Factory API, or being "deployed/run using Nebius AI Cloud compute".
- It has to **use at least one NVIDIA open-source model**.
- The submission should highlight how Nemotron or another NVIDIA open model is used, and where Token Factory sped up the work.
- Entrants give feedback on Token Factory, AI Cloud and the NVIDIA tools used.
- Judging weighs technical implementation, including how effectively the Nebius and NVIDIA models are used.

### Summary
The PR mostly does what it says, and does it unusually well. The measurement section is specific and honest, and every claim about the code that I could check is accurate. Three things should change before merge, and before the submission deadline:
- **F-01:** the Stage 2 diagram presents the two-model judge as what runs, but the live demo runs the stub. That is also the hackathon's "runs on" question.
- **F-02:** one sentence says the Nemotron veto addresses a problem a veto cannot fix.
- **F-05 and F-06:** the new file table is stale and incomplete.

The rest are small wording fixes, and questions about the hackathon framing that only the authors can answer.

### Findings

#### 🟠 Design Issues

**F-01 · `README.md`**
`Stage 2: judge                       ← two open vision models we serve ourselves, NVIDIA Nemotron and Qwen; paid only if both agree (below)`
The diagram describes the two-model judge as Stage 2, but further down the README says "The live demo at survey2earn.xyz still runs the stub", and `api/main.py` defaults to `S2E_JUDGE=stub`. "The live row is the configuration the service runs" has the same problem: it is the configuration the service *can* run. Documentation should describe what is deployed, and a reader skimming the diagram will think the public demo is model-judged. This also matters for eligibility: the rules require the project to be "deployed/run using Nebius AI Cloud compute". Today the Nebius part is the measurement runs and the GPU server, not the public demo, and the README buries that in its last sentence.
**Suggested fix:** say in the diagram what is measured and what is live, and give judges one short paragraph near the top on what runs on Nebius and how to check it. For example:
```
Stage 2: judge   ← two open vision models (NVIDIA Nemotron + Qwen) served on Nebius AI Cloud; paid only if both agree. The public demo still runs the deterministic stub (below).
```
and change "The live row is the configuration the service runs" to "The live row is the service's `S2E_JUDGE=model` configuration". If the model judge can be switched on for the demo before the deadline, that settles the eligibility question better than any wording.

**F-02 · `README.md`**
`- **Still open:** the judge misses 5 very short good descriptions ("Grey office chair on a dark carpet"), which it calls generic, and some quality claims. With a 7B model, instructions alone seem to have reached their limit here, which is why a second, larger model now has a veto (above).`
This sentence contradicts the section above it. The 5 short good descriptions are good descriptions Qwen refuses to pay (false negatives). A veto, as the README itself says, "can only stop a payment, never cause one", so Nemotron's veto cannot recover them: the "Good paid" column stays 68/73 in both combined rows. The veto could only help with the quality claims that get paid. Readers who check the table will notice, and it weakens the otherwise careful "Honest limits".
**Suggested fix:**
```
- **Still open:** the judge misses 5 very short good descriptions ("Grey office chair on a dark carpet"), which it calls generic, and still pays some quality claims. A veto cannot fix the first (it only stops payments); it can catch the second, which is part of why a second model now has one (above).
```

**F-03 · `deploy/judge-gpu.sh`**
`curl -fsSL https://get.docker.com | sudo sh`
This runs an unpinned remote script as root, in a script that otherwise takes supply-chain care: the vLLM image is pinned by digest, and the NVIDIA repo is added with a signed keyring. The machine it runs on handles contributors' photos, which is the whole point of D5. An unpinned script piped to root is the weakest link in that setup.
**Suggested fix:** on Ubuntu, install from the distribution's archive, which apt verifies:
```bash
if ! command -v docker >/dev/null; then
  sudo apt-get update -q && sudo apt-get install -yq docker.io
fi
```
Alternatively, use Docker's apt repository with its signed key, the same way the script already adds the NVIDIA repository.

#### 🟡 Style / Readability

**F-04 · `deploy/judge-gpu.sh`**
`# Then, from the laptop:  ssh -N -L 8001:127.0.0.1:8001 -L 8002:127.0.0.1:8002 emmanuel@<ip>`
A personal login name in a script that is about to be public. Use a placeholder: `<user>@<ip>`.

**F-05 · `README.md`**
`| `deploy/judge-gpu.sh` | Serves the judge model with vLLM on a GPU machine, on 127.0.0.1 only |`
This is stale within this same PR: the script serves two models. The PR description has the same singular wording. Suggested text: "Serves both judge models (Qwen and Nemotron) with vLLM on one GPU, on 127.0.0.1 only".

**F-06 · `README.md`**
`| `src/s2e_validator/judge/chat_client.py` | `ChatCompletionsClient`: the judge's client for a self-hosted, OpenAI-compatible server |`
Missing row: `src/s2e_validator/judge/unanimous.py` is named three times in the README as the module behind the live configuration, but the file table doesn't list it. Add, after this row:
```
| `src/s2e_validator/judge/unanimous.py` | `UnanimousJudge`: several judges; paid only if every one would pay, vetoes marked `JUDGES_DISAGREE` |
```

**F-07 · `README.md`**
`The live row is the configuration the service runs: both models served at once by `deploy/judge-gpu.sh` (42 of 46 GB of GPU memory), every case asked of both. They disagreed on 72 cases, and none of those was paid.`
"46 GB" here and "one 48 GB L40S" in the script's comment both describe the same card. 48 GB is the spec figure; about 46 GB is what `nvidia-smi` reports as usable. Use one figure, or say "(42 of the 46 GB `nvidia-smi` reports)", so nobody wonders whether two different cards were used. (This anchor also carries F-01's wording point.)

**F-08 · `README.md`**
`| Empty or whitespace-only | `TEXT_EMPTY` | |`
Removing every em dash also removed meaning. In the Default columns, "—" meant "no setting", and an empty cell reads as missing data. The same happens for `TEXT_ENCODING_INVALID` and `IMAGE_UNDECODABLE`, and for the status-table rows that now have a blank ID. Write `none` in the Default cells and `n/a` in the ID cells.

**F-09 · `README.md`**
`[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)`
Adding the licence unmodified is right. But the repository carries no copyright statement anywhere, which the Apache appendix recommends: the `Copyright [yyyy] [name of copyright owner]` line is a template for your own files, not the notice itself. Add a short `## License` section at the end of the README (`Copyright 2026 <owner>. Licensed under the Apache License, Version 2.0; see LICENSE.`), or a `NOTICE` file. Don't edit `LICENSE` itself.

**F-10 · PR description (not a file)**
The description says "all four runs side by side" and "serves the model". The README now has two tables (two judges, and five Qwen stages) and the script serves two models. The title's `feat:` prefix doesn't fit a docs-only PR whose commits are all `docs:`. Suggested title: `docs: README for the validator as its own repository, with the measured two-judge setup`. Also, `01af453` and `8327445` have the same subject ("docs: Added Apache 2.0 licence"); it's worth checking the second one isn't an empty or duplicate merge of the licence branch.

#### 🟢 Praise

**`README.md`**
`Honest limits: on this set Nemotron's veto changes at most 1 case of 268, and live the pair scored the same as Qwen alone, so the accuracy is Qwen's.`
This is the best part of the PR. Many write-ups would present the pair as an accuracy gain. This one says the accuracy is Qwen's, names the confounds (Nemotron's reasoning off, a prompt tuned on Qwen), and says Nemotron alone wouldn't be safe. The "read the counts with care" bullet and the held-out pay line do the same. It is what makes the numbers believable.

**`README.md`**
`- **Nebius Token Factory: not used, on purpose.** A hosted inference API would receive contributors' photos, which this project rules out (D5); the client refuses Nebius's hosted API hosts like any other commercial API. Serving the open models ourselves on AI Cloud keeps the photos on a machine we control.`
A principled reason, stated plainly, and the code backs it: `COMMERCIAL_API_HOSTS` in `chat_client.py` really does list the Nebius Studio and Token Factory hosts, along with no redirects and no proxy taken from the environment.

**`deploy/judge-gpu.sh`**
`-p "127.0.0.1:$port:8000" \`
Binding to loopback and reaching it over an SSH tunnel, pinning the image by digest with a comment saying it is the one the numbers came from, and explaining the memory split (FP8 Nemotron, eager mode for the Mamba state) in the comments make this a script someone else can rerun and trust.

#### 🔵 Questions for the Author

**F-11 · `README.md`**
`This repository is the validator on its own. It was built from mid-September 2026 inside the Survey2Earn repository and moved here with its history; issue and pull request numbers written as `#13` refer to that repository, and the Solana program and the demo site live there.`
Does the hackathon allow work that began before its submission period, and does mid-September fall inside that period? If the rules ask for new work, it would help judges to say in one line which parts were built during the hackathon (J2–J4, the two-judge setup, the Nebius deployment) and which already existed (Stage 1, settlement). Please check this against the live rules page; I couldn't fetch it.

**F-12 · `README.md`**
`- **NVIDIA Nemotron Nano 12B v2 VL:** the second judge, with a veto on every payment.`
The judging criteria weigh how effectively the NVIDIA model is used, and the README (rightly) says Nemotron's veto changes at most 1 case. Is there time for one more measured run that gives Nemotron a fair chance, such as reasoning on, or the prompt re-tuned for it? The README already calls those "the next things to measure". If not, I'd keep the honest framing and add what the veto is *for*: a check from an independently trained model against a jailbreak or prompt change that fools Qwen alone. Please don't soften the numbers.

**F-13 · `README.md`**
`- **Nebius Token Factory: not used, on purpose.** A hosted inference API would receive contributors' photos, which this project rules out (D5); the client refuses Nebius's hosted API hosts like any other commercial API. Serving the open models ourselves on AI Cloud keeps the photos on a machine we control.`
The rules reportedly ask for feedback on Token Factory, AI Cloud and the NVIDIA tools used. Is that going on the Devpost form, or should the README carry it? The script comments already hold good material: the BF16 Nemotron build doesn't fit beside Qwen on 48 GB, FP8 runs natively on the L40S, and the hybrid Mamba model needs `--enforce-eager` and a low `--max-num-seqs`. A short "What we learned on AI Cloud" paragraph would turn that into the feedback the rules ask for.

**F-14 · `deploy/judge-gpu.sh`**
`sudo docker run -d --name "$name" --restart unless-stopped --gpus all \`
The script says the models must start "one after the other: each checks the GPU memory that is free when it starts". After a preemption or reboot, though, `--restart unless-stopped` has the Docker daemon start both containers at once. Has a restart been tried? `unless-stopped` probably recovers by restarting a container that runs out of memory, but if it doesn't, a systemd unit that reruns this script (or the start part of it) would keep the order.

### Verdict
**Request Changes**
Small, fast fixes. F-01 (say what is live versus measured) and F-02 (the veto sentence) are worth making before the submission deadline, and the F-05/F-06 file table fixes take a minute.

---

### GitHub Review Comment
> Becomes the summary body of the draft GitHub review (see `draft-payload.md`); also usable as-is in the GitHub "Leave a comment" box.

This is a strong README. The measurement section is specific and candid ("the accuracy is Qwen's"), and every claim about the code I checked (the host refusals, `JUDGES_DISAGREE`, the env vars, the `--combine` flag) matches. Before merging, and before today's 10:00 PT hackathon deadline, I'd fix two things: the Stage 2 diagram should say the public demo still runs the stub, which also bears on the "runs on Nebius AI Cloud" rule (F-01), and the "Still open" bullet credits the veto with fixing missed good descriptions, which a veto can't do (F-02). The rest are small wording fixes and questions about the hackathon framing. Requesting changes, but they're quick.
