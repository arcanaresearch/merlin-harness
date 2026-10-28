# Benchmarking Merlin

> **Local, non-official reproductions.** The results below are not official leaderboard
> submissions. They were produced locally with DeepSeek and the disclosed adaptations needed to
> connect each benchmark to Merlin. Denominators, victims, harnesses, and metrics differ across
> tracks; scores must not be compared across rows as though they came from one shared evaluation.

Merlin is not benchmarked with one dataset or one ASR number. It is benchmarked as an
**adaptive attacker that finds real, executable vulnerabilities under a fixed budget**. Published
results include the material scope, model, oracle, budget or denominator, and adaptation details
needed to interpret them; compact summaries link to those boundaries rather than implying an
official benchmark configuration. No score is published that the harness did not actually earn.

## Local reproduction results

| Track | Verified local result | What the result measures |
| --- | --- | --- |
| RIFT-Bench | 22 runnable systems: probe **3/22 ASR**; Merlin **12/22 ASR**; each **21/22 AAR**. Five additional systems were excluded for upstream or provider incompatibility. | Head-to-head on flow 0 of the Direct Prompt Injection / Task Drift Spam probe family only, with DeepSeek substitutions and adapted dependencies/provider wiring. |
| Jailbreak Foundry | **17/98 = 17.3%** over scored tasks; **17/102 = 16.7%** when four timeouts are included. | DeepSeek victim susceptibility, not a Merlin score. `jailexpert` was unrunnable and excluded because its experience file was absent. |
| MUZZLE + The Zoo | MUZZLE **0/48**; Merlin **0/10** judged rounds across three Merlin campaigns. | One adapted Gitea `gitea1` scenario. Both used the same `deepseek-chat` victim and the same MUZZLE ground-truth judge; attacker models and round counts differed. |
| AgentHarm | Average score **0.305**; refusal **0.4**. | Victim-agent results over five validation behaviors with a DeepSeek judge substitution, not Merlin performance numbers. |
| AIRTBench | Deferred | Requires an approved safe sandbox for benchmark-generated code. |
| REDAgentBench / DianJin-RED | Deferred | Pending a commercial-use decision: benchmark data is CC BY-NC 4.0 and adapted or quoted material remains subject to applicable upstream terms. |

These results provide comparable local evidence where the evaluation contracts overlap, including
a direct RIFT probe-versus-Merlin comparison. They do not establish official rankings or universal
scanner superiority. Raw benchmark and vendor outputs are intentionally excluded from the release;
the release documents only the verified aggregate results and methodology boundaries.

## Track notes

### RIFT-Bench

[RIFT-Bench](https://aclanthology.org/2026.findings-emnlp.1116/) compares dynamic red-teaming
across agentic systems. The local reproduction evaluated the benchmark probe and Merlin on the
same 22 runnable systems, but only for flow 0 of the adversarial-user **Direct Prompt Injection / Task
Drift Spam** probe family: one static-probe lane and one real Merlin GOAT-engine lane per system.
RIFT's evaluators scored parsed MLflow traces. The local run substituted DeepSeek victims and judge
for the upstream Azure deployments and adapted dependency versions and provider wiring; victim
tools, prompts, and logic were not changed. Five other systems could not enter the comparison
because their upstream implementations or required providers were incompatible with the local
environment. The reported 3/22 and 12/22 ASR values therefore use 22, not 27, as their denominator;
AAR was 21/22 for each. This narrow comparison is not an official RIFT-Bench submission.

### MUZZLE + The Zoo

[MUZZLE](https://github.com/gsiros/muzzle) is an automated, adaptive red-teaming framework for web
agents against indirect prompt injection. [The Zoo](https://zoo-eval.github.io/zoo_website/) is a
simulated, interconnected web with deterministic reset and backend observability. In the adapted
local end-to-end runs, both arms evaluated one Gitea `gitea1` delete-repository scenario with the
same `deepseek-chat` browser victim and the same MUZZLE ground-truth judge. MUZZLE used a
`deepseek-chat` attacker and
completed 48 judged PAIR refinement rounds. Merlin used a `deepseek-v4-pro` attacker and completed
10 judged rounds across three real GOAT campaigns. Neither defeated the victim. The MUZZLE path
included deterministic injection-surface and dispatch fallbacks plus provider/DeepSeek compatibility
patches; Merlin deterministically planted each injection on every open issue. These adaptations and
the unequal round counts mean the two zeroes demonstrate end-to-end execution, not scanner
equivalence or a controlled rate comparison.

The framework and its published evaluation are described by
[USENIX Security 2026](https://www.usenix.org/conference/usenixsecurity26/presentation/syros).

### Jailbreak Foundry

[Jailbreak Foundry](https://proceedings.mlr.press/v306/fang26y.html) standardizes runnable text
jailbreak attacks. With JBF's evaluator re-pointed from its default GPT-4o judge to DeepSeek, the
adapted local result was 17/98 scored tasks (17.3%), or 17/102 (16.7%) when four 600-second timeouts
are included in the denominator. The `jailexpert` attack was not runnable because the required
experience file was missing from the clone, so it is outside both denominators. These figures
measure DeepSeek victim susceptibility and must not be attributed to Merlin.

### AgentHarm

[AgentHarm](https://ukgovernmentbeis.github.io/inspect_evals/evals/agentharm/) measures whether a
target agent engages in harmful behavior. The local subset was five validation behaviors—IDs 2-1,
6-1, 7-1, 8-1, and 10-1—using the upstream rubrics and aggregate metrics. The victim was DeepSeek,
and the judge backend was substituted from the upstream GPT-4o default to DeepSeek. The average
score of 0.305 and refusal score of 0.4 characterize that victim agent, not Merlin's ability to
discover vulnerabilities.

### Deferred tracks

- [AIRTBench](https://arxiv.org/html/2506.14682) is deferred until benchmark-generated code can run
  in an approved safe sandbox.
- [REDAgentBench / DianJin-RED](https://github.com/aliyun/qwen-dianjin/tree/master/DianJin-RED) is
  deferred pending a commercial-use decision. Its
  [data license](https://github.com/aliyun/qwen-dianjin/blob/master/DianJin-RED/DATA_LICENSE.md)
  applies CC BY-NC 4.0 to the benchmark cases and provenance records except where an upstream
  notice states otherwise; adapted or quoted source material retains applicable upstream terms.

## Continuing benchmark stack

The local reproductions are only part of the evaluation program. A rotating hidden executable
Merlin suite remains the primary product benchmark across browser, API, tools, RAG, authentication,
sessions, tenants, and stateful workflows. Public tracks provide reproducibility and comparison,
while the hidden suite reduces overfitting to known cases. AgentDojo remains a useful secondary
victim-agent regression track; it does not by itself measure the complete quality of Merlin as a
harness.
