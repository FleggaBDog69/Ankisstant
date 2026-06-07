---
name: anki-browse
description: Use this skill whenever the user wants to find existing Anki cards for a topic, generate Anki search terms, decompose a compound clinical statement into searchable concepts, suggest tag-name keywords, or classify a topic into a hierarchical tag path. Triggers include find cards on X, search my deck for X, what search terms for X, tag this topic, classify this topic. Designed for a Year 3 Australian medical student's cloze deck. Do NOT use this skill to create or score cards.
---

# Anki Browse — Search Terms, Tag Keywords & Topic Classification

## Overview

This skill turns a topic (or a messy chain-of-reasoning note) into the artefacts the user needs to locate and organise existing cards in their Anki deck:

1. **Search terms** — specific strings that surface cards genuinely about the topic.
2. **Tag keywords** — canonical tag-name fragments for the topic and its tightly-coupled entities.
3. **A hierarchical tag path** — `{system}::{subsystem}::{topic}` for filing the topic.

Pick the mode that matches the request. Output is always machine-readable (JSON), no prose, unless the user explicitly asks for explanation.

## Mode 1 — Search terms

Given a topic, return a JSON array of **3 to 8 highly specific** search strings that surface cards genuinely about that topic — not cards that merely mention it in passing.

### Decomposition (critical)

If the input combines **more than one distinct clinical concept** — joined by "and", commas, semicolons, or expressed as a chain of reasoning failures ("failed to recognise X, missed Y, didn't act on Z") — first **split** it into the underlying atomic concepts, then generate search terms for each. Don't try to match the whole compound statement with one search: the deck almost certainly has the pieces scattered across separate cards.

Examples:
- `MRI is first line for HA with neuro deficit, failed to recognise red flags` → `{headache red flags}` AND `{imaging for headache with focal neuro deficits}` as separate concept groups.
- `aortic stenosis murmur and indications for valve replacement` → `{aortic stenosis murmur characteristics}` AND `{AVR indications}`.
- Single concept like `McDonald criteria` → no decomposition; generate variants on that one concept.

### Rules

- Prefer multi-word phrases and eponyms over single common words.
- Avoid generic 1–3 letter abbreviations on their own (`MS`, `DM`, `IV`) — they collide with too many unrelated cards. Disambiguate: `multiple sclerosis`, `McDonald criteria`, not `MS`.
- Include classic exam-relevant entities (pathognomonic signs, key drugs, diagnostic criteria, eponyms) — but **only** if tightly bound to the topic.
- Narrow single-concept topics → 3–4 terms. Decomposed multi-concept inputs → 2–3 terms per concept (up to 8 total). Quality over quantity.

**Output:** ONLY a JSON array of strings. No prose, no quotes around the array.

### Rescoping (broader / narrower)

When given a topic, a previous set of terms, and a directive:

- **BROADER** — pull back a level of abstraction: adjacent / parent concepts, the wider disease category, related syndromes. Replace narrow eponyms with their umbrella category.
- **NARROWER** — drill in: more specific sub-entities, drug names, exact diagnostic criteria, named signs, specific complications. Replace umbrella categories with their highest-yield sub-entries.

Same JSON-array-of-strings format. 3–6 items. No prose.

## Mode 2 — Tag keywords

Given a topic, return a JSON array of objects matching tag names for **that topic and its tightly-coupled disease entities only**.

### Stay tight (most important rule)

- Return the topic itself, plus **at most a couple** of closely-related disease entities or direct differentials (e.g. for `multiple sclerosis`: `multiple_sclerosis`, `optic_neuritis`).
- **Do not** branch out to individual signs, symptoms, labs, investigations, or buzzwords. For `multiple sclerosis` that means NO `oligoclonal_bands`, NO `Uhthoff`, NO `MRI` — those flood results with tangents. Only widen to a separate condition, never to a feature of the topic.
- 2 to 4 items, fewer is better.

**Format** — JSON array of objects exactly like:

```json
[{"keyword": "multiple_sclerosis", "resource": "Boards & Beyond — Neuro: MS", "step": "Step 1"}]
```

- `keyword` is matched case-insensitively as a substring against existing tag names → prefer the canonical form (snake_case or PascalCase, no spaces).
- Avoid bare 1–3 letter abbreviations; disambiguate (`multiple_sclerosis`, not `MS`).
- `resource` is a concise study reference (Boards & Beyond chapter, Pathoma section, First Aid page-range).
- `step` is one of `Step 1`, `Step 2 CK`, `Step 3`, or `Step 1+2`; use `AMC` for Australian-context post-graduation entries.

**Output:** ONLY the JSON array. No prose.

## Mode 3 — Hierarchical classification

Given a topic, concept, or source text, return a JSON object filing it into a tag path:

```json
{"system": "...", "subsystem": "...", "topic": "..."}
```

- **system** — top-level body system or domain: Cardio, Neuro, Endo, GI, Resp, Renal, Heme, MSK, Derm, Repro, Psych, ID, Onc, Pharm, Stats, Genetics, Biochem, Immuno. Pick the single best fit.
- **subsystem** — more specific anatomy / disease category within the system (Arrhythmias for Cardio, Stroke for Neuro, Diabetes for Endo).
- **topic** — the most specific clinical entity, drug, sign, or mechanism (AFib, MCA_stroke, Digoxin, McDonald_criteria).
- Use PascalCase or snake_case (no spaces, no `::`, no slashes).
- Avoid generic placeholders (`General`, `Misc`, `Other`). If a level is genuinely unclear, return an empty string for it — the caller will skip it.

**Output:** ONLY the JSON object. No prose, no markdown fences.

## What not to do

- **Do not create cards** — this is a find/organise skill. Card generation belongs to the `anki-cards` skill.
- **Do not score cards** — that's the `anki-card-scorer` skill.
- **Do not pad** the term/keyword lists to hit a count — fewer, tighter results beat broad coverage.
- **Do not add prose** around the JSON unless the user explicitly asks for an explanation.
