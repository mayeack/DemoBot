---
layout: default
title: Home
nav_order: 1
description: "One Cisco — Governing Agentic AI End to End. The FY'27 Agentic AI Governance Workshop."
permalink: /
---

# One Cisco: Governing Agentic AI End to End
{: .fs-9 }

The Agentic AI Governance Workshop — FY'27
{: .fs-6 .fw-300 }

**Secure. Observable. Governed. Measurable. One Cisco, end to end.**

A field workshop for the executives accountable for AI — and the engineers who run it. Observability is one of four governed pillars covered here, not the whole story.

[Start: Setup & Prerequisites](setup.html){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
[Jump to the labs](section-0-overview.html){: .btn .fs-5 .mb-4 .mb-md-0 }

{: .warning }
> **Draft — FY'27 first cut.** This site is generated from the workshop narrative and facilitator guide. Commands here are reconciled against the [DemoBot repo](https://github.com/mayeack/DemoBot), but **verify them against your own environment** before delivering. Never invent customer names, ROI percentages, or quotes.

---

## The Problem: AI Is Moving Faster Than Governance

Enterprises are shipping agentic AI into production faster than they can govern it. Autonomous and semi-autonomous agents now make decisions, call tools, and generate language that reaches customers, patients, and regulators — at machine speed, around the clock, at a volume no human review queue can keep pace with.

The risk is not theoretical. A single AI interaction can leak PII or PHI, absorb a prompt injection that overrides its instructions, fabricate a medical treatment that never existed, or quietly drift away from the behavior it was certified with. Each of these is, simultaneously, a **security** event, an **operations** event, a **quality** event, and a **compliance** event.

Yet most organizations try to govern this with disconnected point solutions. The security team sees a blocked prompt in one console. The SRE sees a latency spike in another. The data-science team sees a quality score in a third. The compliance officer, during an audit, is handed screenshots from all three and asked to reconstruct what happened on a given turn. **Four tools, four truths, no single thread connecting them.**

That gap — between the speed of agentic AI and the speed of governing it — is the problem this workshop closes.

---

## The One Cisco Thesis

One Cisco closes the governance gap end to end with **one integrated architecture across four pillars** — and the differentiator versus point tools is structural, not cosmetic:

{: .note }
> **Every AI interaction is captured once and correlated on a shared, OTel-compliant identifier (`gen_ai.event.id` / `trace_id`), so security, operations, quality, and audit become one investigation, not four disconnected tools.**

### The four pillars

| Pillar | Capability | Platform |
| --- | --- | --- |
| **Measure / Evaluate** | Define **good**, then prove it — baseline vs. poisoned behavior, token & cost, signals that surface unknown unknowns, continuous metrics on a deployed agent | Cisco Agent Observability |
| **Secure** | Runtime policy + guardrails on every prompt and response | Cisco AI Defense |
| **Observe** | End-to-end tracing, latency, and cost | Splunk Observability Cloud |
| **Govern** | Immutable audit trail + forensics + security incident response | Splunk Core / Enterprise Security |

### The single pane of glass

The architectural anchor is the **AI Governance Overview** dashboard in Splunk Core. It answers, in one view, the question every leader is actually asking — *"Is our AI safe, reliable, accurate, and accountable right now?"* — through KPI tiles (Total AI Requests, Cost, Token Usage, Unique Sessions), safety tiles (Safety Violations, PII Detected, Policy Blocked, Guardrails Triggered), GenAI Detection summaries, cost / latency / volume trends, and a Recent AI Requests log you pivot from to any turn's correlated record. Pillar deep-dives — Prompt Injection Detection, PII Detection, Tokenomics — are one click away.

---

## Start with Evaluation: Define "Good" Before You Can Guard It

Before any guardrail can fire, someone has to define what the guardrail is *for*. That definition is an **evaluation** — and in this architecture it comes first, because **a guardrail is just an evaluation finding with an action attached.** You cannot block what you have not measured, and you cannot certify what you cannot define.

**Cisco Agent Observability** is where agentic behavior gets defined and measured. It evaluates the *whole agent trace* — workflow, agents, tool calls, and the LLM response — and scores each turn against research-backed metrics (hallucination, context adherence, PII/PHI leakage, tool-selection quality) plus **custom metrics you define for your own domain**. Those metrics are run by **Luna**, Cisco's family of small, purpose-built evaluation models, so you get LLM-as-judge quality *without* paying frontier-model prices to score every turn — which is exactly what makes evaluation affordable to run **continuously**, not just once. You run it two ways: as an **offline experiment** over a dataset before you ship, and as **continuous scoring** on live traffic once deployed, where its **signals surface the unknown unknowns** no one anticipated.

{: .positioning }
> Cisco Agent Observability is the quality/evaluation pillar — **a Cisco product** (formerly Galileo, now owned by Cisco). Position it as Cisco IP, never as third-party. Splunk Observability Cloud and Splunk Core are **distinct** products. Cisco AI Defense is the **live** runtime-security layer.

---

## The Journey: One Turn, Four Pillars, One Thread

The workshop is delivered against a real, running application: **MedAdvice** — a multi-agent medical-advice chatbot (FastAPI + LangGraph), built on the **DemoBot** engine and emitting telemetry under the OTel service name `demobot-v3`. The same engine re-skins to six verticals (medical, tax, benefits, legal, finance, telecom) — proof that the governance pattern generalizes far beyond healthcare.

The flow follows a single governed turn through all four pillars:

| Step | Pillar | What happens | Page |
| --- | --- | --- | --- |
| 0 | **Overview** | The exec opens the single pane of glass — every governed turn on one screen | [Section 0](section-0-overview.html) |
| 1 | **Measure** | Baseline vs. poisoned eval defines "good," catches the unknown unknowns | [Lab 1](lab-1-measure.html) |
| 2 | **Secure** | The eval finding becomes a runtime guardrail: blocked → compliant, live | [Lab 2](lab-2-secure.html) |
| 3 | **Observe** | A latency SLO breach is traced end-to-end to its root cause | [Lab 3](lab-3-observe.html) |
| 4 | **Govern** | The immutable audit trail proves it held; injection escalates to ES | [Lab 4](lab-4-govern.html) |

Each pillar measures against the baseline the evaluation set in Part 1. AI Defense enforces that definition, Observability Cloud confirms the agent honors it under load, and Splunk Core proves it held.

---

## One Turn, End to End

The differentiator is easiest to see on a single worked turn. Because every pillar is integrated, the four stories are not four separate investigations — they are four facets of *one event*:

1. **Measure.** Cisco Agent Observability turns answer quality, token cost, and risk from a subjective "vibe" into continuously measured, SLA-governed metrics — scored cheaply by Luna, with signals that surface the unknown unknowns before they reach a customer.
2. **Secure.** Cisco AI Defense inspects the prompt and response against the policies and guardrails; the verdict and any policy block are attached — including the custom guardrail promoted directly from the measurement above.
3. **Observe.** In Splunk Observability Cloud, that trace shows where the latency went across the service — the *operational* face of the event.
4. **Govern.** Every governed AI interaction lands in an immutable, fully correlated audit trail — so proving compliance becomes a single query, and evidence-backed threats like prompt injection flow straight into the existing security response workflow.

One turn. Four pillars. One thread. The data scientist, the security analyst, the SRE, and the auditor are all looking at the same event — which is exactly what "one investigation, not four" means in practice.

---

## Five Executive Outcomes

| Outcome | What it means | Grounded in |
| --- | --- | --- |
| **Unified Visibility & Control** | The whole AI program on one screen; every KPI live, every number one click from its evidence | single pane of glass (Section 0) |
| **Improved Outcomes** | Measurable quality, cost, and risk; optimized agent behavior, unknown unknowns surfaced | evaluation, signals & continuous metrics (Lab 1) |
| **Trusted AI** | Safe, compliant responses; non-compliant output blocked at runtime | exposed → compliant live (Lab 2) |
| **Operational Excellence** | Reliable, cost-efficient AI; trace, don't guess | APM trace-to-root-cause (Lab 3) |
| **Accountability & Evidence** | End-to-end auditability; defensible, correlated evidence | immutable audit trail for every event (Lab 4) |

---

## The Call to Action

Agentic AI is already in production. The question for every CISO, CIO, CTO, and Chief Risk and Compliance officer is no longer *whether* to govern it, but *whether you can prove you are.*

One Cisco makes that proof a single screen and a single thread. **Capture every AI interaction once. Correlate it across security, operations, quality, and audit. Investigate once, not four times.**

**Secure. Observable. Governed. Measurable. One Cisco, end to end.** Let's govern AI at machine speed.

---

*Owners: Matt Poland (Sr. Director, Solutions Engineering) · Michael Yeack (Solutions Engineer) · Gerry D'Costa (AI Solutions Architect). FY'27 AI roadshow.*
