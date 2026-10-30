# Feedback on Nebius and NVIDIA tools

From building and measuring the Survey2Earn judge, October 2026.

## Nebius AI Cloud

- **Preemptible L40S.** It fits our work: judging runs in batches, and the machine is off between them. But it was reclaimed repeatedly, twice within minutes of starting a measurement, with no warning. A warning before a VM is reclaimed, or a guide to making long jobs resumable, would have saved several runs.
- **Sharing access.** An invited teammate saw "Tenant unavailable" until the right IAM group and roles were found. A ready-made "can start and stop this VM" role in the invite flow would help small teams.
- **Console.** The boot disk form asked for at least 1,280 GiB with the NVIDIA image, while a VM created through the CLI had 100 GiB. The VM page also showed "Internal server error" in its Resource capacity section.

## Nebius Token Factory

Not used, on purpose. A hosted inference API would receive contributors' photos, which this project rules out. What would change that: a stated no-retention guarantee for image inputs, and open vision models available as a scale-to-zero endpoint, so occasional judging needs no VM.

## NVIDIA Nemotron Nano 12B v2 VL

- **Fitting it.** The BF16 build next to a 7B model leaves no working memory on an L40S; NVIDIA's FP8 build fits (42 of the 46 GB `nvidia-smi` reports). As a hybrid (Mamba) model it reserves state per concurrent request, so it needed `--max-num-seqs 8` and `--enforce-eager` to start. A note on this in the model card would help.
- **Naming.** The repository name in the vLLM recipe and in blog posts differs (`NVIDIA-Nemotron-Nano-12B-v2-VL-BF16` against `Nemotron-Nano-12B-v2-VL-BF16`).
- **As a judge.** With the reply forced to JSON, so its reasoning off, it paid every good description but most generic text too, often at full marks (193 of 268 agreed alone). We did not measure it with reasoning on: at single-request speed that was too slow for our deadline. Guidance on reasoning with structured output would help anyone building evaluators.

## NVIDIA L40S and tooling

CUDA drivers and the container toolkit came ready on the Nebius image, and FP8 ran natively, which is what made two models on one card possible.
