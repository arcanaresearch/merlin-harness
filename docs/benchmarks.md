# Benchmarking Merlin

> **Local, non-official reproductions.** The results below are not official leaderboard
> submissions. They were produced locally with DeepSeek and the disclosed adaptations needed to
> connect each benchmark to Merlin. Denominators, victims, harnesses, and metrics differ across
> tracks; scores must not be compared across rows as though they came from one shared evaluation.

Merlin is not benchmarked with one dataset or one ASR number. It is benchmarked as an
**adaptive attacker that finds real, executable vulnerabilities under a fixed budget**. Every
result must state the target, objective, budget, oracle, and what was verified versus what remains
unverified. No score is published that the harness did not actually earn.

## Local reproduction results

| Track | Verified local result | What the result measures |
| --- | --- | --- |
| RIFT-Bench | 22 runnable systems: the benchmark probe reached **3/22 ASR**, while the Merlin engine reached **12/22 ASR**. Both recorded **21/22 AAR**. Five additional systems were excluded because of upstream or provider incompatibility. | A head-to-head local reproduction on the same 22 runnable systems. This is not an official RIFT-Bench submission. The excluded systems are not counted as failures. |
| Jailbreak Foundry | **17/98** | Susceptibility of the DeepSeek victim under the adapted local evaluation. This is a victim-model result, not a Merlin score. |
| MUZZLE + The Zoo | MUZZLE **0/48**; Merlin **0/10** | Both paths ran end to end, but neither defeated the same DeepSeek browser victim. The unequal attempt counts are disclosed and the two zeroes do not establish scanner equivalence. |
| AgentHarm | Average score **0.305**; refusal **0.4** | Behavior of the victim agent. These are victim-agent measurements, not Merlin performance numbers. |
| AIRTBench | Deferred | Requires an approved safe sandbox for benchmark-generated code. |
| REDAgentBench | Deferred | Pending a decision on commercial use under the benchmark's CC BY-NC-ND license. |

These results provide comparable local evidence where the evaluation contracts overlap, including
a direct RIFT probe-versus-Merlin comparison. They do not establish official rankings or universal
scanner superiority. Raw benchmark and vendor outputs are intentionally excluded from the release;
the release documents only the verified aggregate results and methodology boundaries.

## Track notes

### RIFT-Bench

[RIFT-Bench](https://aclanthology.org/2026.findings-emnlp.1116/) compares dynamic red-teaming
across agentic systems. The local reproduction evaluated the benchmark probe and Merlin on the
same 22 runnable systems. Five other systems could not enter the comparison because their upstream
implementations or required providers were incompatible with the local environment. The reported
3/22 and 12/22 ASR values therefore use 22, not 27, as their denominator; AAR was 21/22 for each.

### MUZZLE + The Zoo

[MUZZLE](https://github.com/gsiros/muzzle) is an automated, adaptive red-teaming framework for web
agents against indirect prompt injection. [The Zoo](https://zoo-eval.github.io/zoo_website/) is a
simulated, interconnected web with deterministic reset and backend observability. In the adapted
local end-to-end runs, MUZZLE completed 48 attempts and Merlin completed 10 against the same
DeepSeek browser victim. Neither produced a successful attack. Different attempt budgets prevent
the raw fractions from serving as a controlled rate comparison.

The framework and its published evaluation are described by
[USENIX Security 2026](https://www.usenix.org/conference/usenixsecurity26/presentation/syros).

### Jailbreak Foundry

[Jailbreak Foundry](https://proceedings.mlr.press/v306/fang26y.html) standardizes runnable text
jailbreak attacks. The adapted local result, 17 successful cases out of 98, measures how often the
DeepSeek victim was susceptible under that evaluation. It must not be attributed to Merlin.

### AgentHarm

[AgentHarm](https://ukgovernmentbeis.github.io/inspect_evals/evals/agentharm/) measures whether a
target agent engages in harmful behavior. The local average score of 0.305 and refusal score of 0.4
characterize the evaluated victim agent, not Merlin's ability to discover vulnerabilities.

### Deferred tracks

- [AIRTBench](https://arxiv.org/html/2506.14682) is deferred until benchmark-generated code can run
  in an approved safe sandbox.
- [REDAgentBench](https://arxiv.org/html/2608.10669) is deferred pending a commercial-use decision
  for its CC BY-NC-ND licensing terms.

## Continuing benchmark stack

The local reproductions are only part of the evaluation program. A rotating hidden executable
Merlin suite remains the primary product benchmark across browser, API, tools, RAG, authentication,
sessions, tenants, and stateful workflows. Public tracks provide reproducibility and comparison,
while the hidden suite reduces overfitting to known cases. AgentDojo remains a useful secondary
victim-agent regression track; it does not by itself measure the complete quality of Merlin as a
harness.
