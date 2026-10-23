---
name: humanizer
kind: portable
version: 4.0.0
disable-model-invocation: true
description: >
  Remove signs of AI-generated writing from text, guide Claude to write naturally from
  the start, or scan a draft for AI tells without rewriting it. Use this skill whenever
  the user wants to "humanize" text, asks you to rewrite something to sound less like AI,
  says output feels robotic or generic, or wants writing that coworkers, partners, or the
  public will read to sound natural — and when reviewing drafts of emails, Slack messages,
  technical docs, READMEs, guides, specs, or stakeholder communications for AI tells.
  Trigger on phrases like: "make this sound human", "de-AI this", "humanize", "sounds too
  much like AI", "clean up the AI voice", "write this naturally", "scan this for AI
  tells", "detect, don't rewrite", or any request where the output shouldn't read as
  machine-generated. Even if the user doesn't use those exact words, use this skill
  whenever writing quality and natural voice matter for the deliverable. NOT for
  financial or market digests (use `humanizer-finance`), and NOT for writing new
  content from scratch when voice is not a concern.
---

# Humanizer

Make AI-assisted writing sound like a person wrote it. Works in three modes:

- **Editing mode**: User pastes existing text. Rewrite it.
- **Generation mode**: User asks you to write something new. Apply these principles as you write — don't generate slop and then fix it.
- **Detect-only mode**: User wants a scan/report of AI tells, not a rewrite — e.g., a pre-publish lint pass. List matches with quotes and one-line fixes; don't rewrite.

Detect which mode based on context. If the user pastes a block of text or says "rewrite/edit/humanize this," it's editing mode. If they ask you to draft, write, or compose something, it's generation mode. If they ask to "scan," "detect," "flag," "check," or "lint" text for AI tells without wanting a rewrite, it's detect-only mode.

## Core philosophy

AI writing fails in two ways: obvious pattern tells (the "slop" markers everyone recognizes) and soullessness (technically clean but clearly no human behind it). You need to fix both.

Soullessness is the harder problem. A rewrite that strips AI tells but reads like a press release is still obviously AI. Good writing has a point of view, varies its rhythm, and occasionally surprises the reader.

## Hard constraints

These apply in every mode. They aren't style preferences — violating one is a functional bug in the output, not a taste difference.

**No fabrication.** Never add a fact, name, number, date, quote, or citation that isn't in the source text or supplied by the user. You may add an opinion, reaction, or transition — you may not add a factual claim.

**Evidence boundaries.** When the source material is vague and making it more specific would mean inventing something, don't launder the vagueness into a confident-sounding claim. Flag the gap, ask the user, or use an explicit bracketed placeholder (e.g., "[confirm the exact figure]") instead of guessing.

**Defensibility test.** Before removing or rewriting anything, decide whether it's slop (adds no meaning, follows a formula) or voice (a deliberate choice the author could defend if asked). If the author could defend it, it's voice — leave it.

**Scope limit.** Editing mode is phrasing-level: fix the words, not the argument. Don't restructure, re-order, or re-argue the substance unless the user explicitly asks for that. One tell fixed cleanly beats three fixed clumsily — stop editing once what's left is voice, not slop.

**Brief vs. artifact.** If the input includes instructions or editorial notes alongside the text to rewrite (e.g., "make this punchier: <draft>"), treat the instructions as instructions. Never reprint them as if they were part of the output — that's the same class of error as inventing content.

**File-type refusal.** If asked to run this skill against a code, config, or data file (`.js`, `.py`, `.json`, `.yaml`, etc.) rather than prose, refuse the rewrite and say why — humanizer edits prose, and rewriting code as if it were English could break it.

## Process

### Editing mode

1. Read the input text.
2. If the user supplies a voice sample or reference prose (their own past writing, a style guide, an example they like), it takes priority over the default rules below. Match its cadence and vocabulary — for example, if the sample uses em dashes freely, don't strip them to zero just because pattern 12 flags overuse in AI-generated text.
3. Identify the document type and audience (Slack message? Technical doc? Stakeholder email?) and calibrate your rewrite accordingly:
   - **Slack/Teams messages**: Keep short. Contractions are fine. Personality is welcome. Don't over-polish — a Slack message that reads like a blog post is its own kind of uncanny valley.
   - **Technical documentation**: Clarity and precision matter more than personality. Strip AI filler and promotional language, but don't inject forced casualness. Straightforward and specific beats clever.
   - **Emails to stakeholders/partners**: Professional but not robotic. Have a clear point. Get to it faster than AI usually does.
4. Scan for tells and collect candidates — don't edit yet. For each candidate, apply the defensibility test above: if the author could defend it, it's voice, leave it; if removing it loses no meaning, it's slop, fix it.
5. Rewrite the text: fix the validated candidates, preserve the core meaning and every fact, match the appropriate register. Stay within the scope limit above — phrasing only, don't restructure the argument unless asked.
6. Do one final read-through asking two questions: "If I received this from a colleague, would anything strike me as weird or generated?" and "Did the rewrite add or remove any fact, name, number, date, or claim versus the source?" Fix anything that fails either check — treat an unsupported addition or a lost claim as an error, not a stylistic choice.
7. Output the clean rewrite, followed by a brief summary of what you changed and why (2-5 bullet points, not an exhaustive list — just the notable fixes).

### Generation mode

The hard constraints above still apply — don't invent facts, numbers, or citations while drafting. Apply the pattern catalog principles as you write. The goal is to never produce the AI tells in the first place. Key things to keep in mind while generating:

- Lead with the actual point. Don't warm up with "In today's rapidly evolving landscape..."
- Use "is" and "are" and "has" — don't contort sentences to avoid simple verbs
- Be specific. Concrete details beat vague claims about significance every time.
- Vary sentence length naturally. Not every sentence needs to be the same structure.
- Skip the generic positive conclusion. End when you've said what you need to say.
- If the topic is one where you'd have an opinion or reaction, let that come through.
- After drafting, do a quick self-check for residual AI patterns and clean them up.
- Output the text, followed by a brief note on any choices you made (tone, what you emphasized, etc. — 1-3 sentences, only if genuinely useful context).

### Detect-only mode

Use this when the user wants a lint pass, not a rewrite — "scan this for AI tells," "check before I ship this," "detect only, don't rewrite."

1. Read the input text.
2. Scan for pattern matches from the catalog below, applying the "What not to flag" guardrails so you don't report false positives.
3. For each real match, quote the offending phrase, name the pattern, and give a one-line fix — don't rewrite the whole passage.
4. Weight confidence: report higher-tier and clustered patterns (see pattern 7's tiering) with more confidence than isolated, low-signal instances.
5. Output a short list — pattern name, quoted text, one-line fix — and nothing else. No rewritten draft unless the user asks for one after seeing the report.


## What "human" actually sounds like

Stripping AI patterns is necessary but not sufficient. Here's what to aim for:

**Have a point of view.** Real people have opinions. "I think this approach is better because..." is more human than neutrally listing options.

**Vary your rhythm.** Short sentences. Then longer ones that take their time. Mix it up — monotone sentence length is a tell even when the words are fine.

**Be comfortable with uncertainty.** "I'm not sure this is the right call, but here's my thinking" is more credible than false confidence.

**Be specific about what you mean.** Not "this raises concerns" but "this means we'd need to rewrite the auth module, which took three weeks last time."

**Apply the portability test.** If a sentence could move unchanged into someone else's writing about a different person, company, or product, it's probably filler. Quick check: swap the key nouns for an unrelated domain — if the sentence still reads fine, it isn't saying anything specific about this one.

**Apply the sentence-load test.** Every sentence should carry a claim, a number, a named detail, a mechanism, or a consequence. A sentence that only adds transition or rhythm, without any of those, is a cut candidate — good rhythm comes from varying sentences that already carry weight, not from padding.

**Name the mechanism, not the feeling.** Say what something does, not how it feels — name the mechanism or a number instead of an abstraction. Watch for nouns that sound technical but usually have a plainer, more concrete word underneath: substrate, wedge, vector, locus, vantage, nexus, bedrock, flywheel, north star, gold-plating, ratchet. If a plainer word says the same thing, use it.

**Let some imperfection in.** Perfect parallel structure in every paragraph feels algorithmic. Occasional tangents, asides, and conversational moves are human.

**End when you're done.** Don't add a conclusion paragraph that restates everything you just said. If the email is three sentences, that's fine.


## What not to flag

Not every human tic is an AI tell, and over-flagging erodes trust in the rest of this skill. Before flagging something, check it isn't one of these:

- **Clean grammar isn't a tell.** Correct, well-formed sentences are just correct — grammatical precision alone says nothing about authorship.
- **Domain terminology isn't jargon.** A precise technical or domain-specific term ("weighted interval score," "eviction," "quorum") is expected vocabulary, not an AI cluster word. The tell is generic business buzzwords (leverage, synergy, holistic, streamline, robust) leaking into technical prose — not a domain's own correct terms.
- **Formal or elevated vocabulary isn't a tell by itself.** Some people and some registers are just formal. Judge the whole passage, not one word choice.
- **One em dash or curly quote isn't a tell.** Patterns 12 and 17 below are about overuse and clustering — a single instance in a long piece is normal human writing, not a sign of anything.
- **Deliberate passive voice isn't a tell.** Passive is correct when the actor is unknown, unimportant, or already established ("the servers were rebooted at 2am"). Flag passive used to dodge naming an actor that matters, not passive in general.
- **Don't over-edit already-good prose.** If a rewrite pass changes more than roughly a fifth of a passage that had no real problems, that's a sign of overcorrection, not improvement — dial back to the validated candidates from the collect-then-validate step.

Flag based on clusters of the patterns below, not isolated instances, unless a single instance is high-severity on its own (see the tiering in pattern 7).


---


## AI writing pattern catalog

25 documented patterns sourced from [Wikipedia:Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing) (maintained by WikiProject AI Cleanup), plus additional patterns observed in professional/business contexts.

Don't mechanically check every pattern — internalize the principles and apply judgment based on the document type and audience. Patterns 1-10 are the highest-signal tells; 11-25 are supporting patterns that reinforce the detection. See "What not to flag" above before treating any single instance as a tell.


### Content patterns (1-6)

**1. Significance inflation**
Watch for: stands/serves as, is a testament/reminder, a vital/significant/crucial/pivotal/key role/moment, underscores/highlights its importance, reflects broader, symbolizing its ongoing/enduring/lasting, setting the stage for, evolving landscape, indelible mark, deeply rooted

AI puffs up importance by adding claims about how things "represent broader trends" or "mark pivotal moments." If something is genuinely important, state the facts and let the reader decide.

> Before: "The Statistical Institute of Catalonia was officially established in 1989, marking a pivotal moment in the evolution of regional statistics in Spain."
> After: "The Statistical Institute of Catalonia was established in 1989 to collect and publish regional statistics independently from Spain's national statistics office."

**2. Notability hammering**
Watch for: independent coverage, local/regional/national media outlets, written by a leading expert, active social media presence

AI insists on telling you how notable something is instead of showing you through specific, relevant details.

> Before: "Her views have been cited in The New York Times, BBC, Financial Times, and The Hindu. She maintains an active social media presence with over 500,000 followers."
> After: "In a 2024 New York Times interview, she argued that AI regulation should focus on outcomes rather than methods."

**3. Superficial -ing analysis**
Watch for: highlighting/underscoring/emphasizing..., ensuring..., reflecting/symbolizing..., contributing to..., cultivating/fostering..., showcasing...

Participial ("-ing") phrases tacked onto sentences to simulate depth. They almost never add real information.

> Before: "The temple's color palette resonates with the region's natural beauty, symbolizing Texas bluebonnets and the Gulf of Mexico, reflecting the community's deep connection to the land."
> After: "The temple uses blue, green, and gold. The architect said these reference local bluebonnets and the Gulf coast."

**4. Promotional / brochure language**
Watch for: boasts a, vibrant, rich (figurative), profound, showcasing, exemplifies, commitment to, natural beauty, nestled, in the heart of, groundbreaking (figurative), renowned, breathtaking, must-visit, stunning

AI drifts into tourism-brochure or marketing-copy register, especially for places, organizations, and cultural topics.

> Before: "Nestled within the breathtaking region of Gonder, Alamata Raya Kobo stands as a vibrant town with a rich cultural heritage and stunning natural beauty."
> After: "Alamata Raya Kobo is a town in the Gonder region of Ethiopia, known for its weekly market and 18th-century church."

**5. Vague attributions**
Watch for: Industry reports, Observers have cited, Experts argue, Some critics argue, several sources/publications

AI attributes claims to unnamed authorities instead of citing specific sources. In professional writing, either name the source or drop the attribution.

> Before: "Experts believe it plays a crucial role in the regional ecosystem."
> After: "A 2019 survey by the Chinese Academy of Sciences found it supports several endemic fish species."

**6. Formulaic "challenges and future" sections**
Watch for: Despite its... faces several challenges..., Despite these challenges, Challenges and Legacy, Future Outlook

AI appends boilerplate challenge/opportunity sections. If challenges are worth mentioning, be specific about what they are and what's being done.

> Before: "Despite its industrial prosperity, Korattur faces challenges typical of urban areas, including traffic congestion and water scarcity. Despite these challenges, Korattur continues to thrive."
> After: "Traffic congestion increased after 2015 when three new IT parks opened. The municipal corporation began a stormwater drainage project in 2022 to address recurring floods."


### Language and grammar patterns (7-11)

**7. AI vocabulary clusters**
These words appear at dramatically higher rates in post-2023 text. Tier your confidence rather than treating every word the same:
- **Evidence-grade** (near-definitive even alone): testament, tapestry (abstract), intricate/intricacies, underscore (verb), delve, interplay, multifaceted, holistic, realm. One strong hit from this tier is worth flagging on its own.
- **Wordiness-grade** (needs clustering to count): Additionally, align with, crucial, emphasizing, enduring, enhance, fostering, garner, highlight (verb), key (adj.), landscape (abstract), pivotal, showcase, valuable, vibrant, streamline, leverage (verb), robust, comprehensive, nuanced. Common enough alone that one use isn't meaningful — the tell is several clustering together.

> Before: "Additionally, a distinctive feature is the incorporation of camel meat. An enduring testament to Italian colonial influence is the widespread adoption of pasta in the local culinary landscape."
> After: "Somali cuisine also includes camel meat, which is considered a delicacy. Pasta, introduced during Italian colonization, remains common in the south."

**8. Copula avoidance & false agency**
Watch for: serves as, stands as, marks, represents [a], boasts, features, offers [a] — and its cousin, false agency: handing human verbs to inanimate or abstract things so nobody has to name the actor (the data tells us, the process rewards patience, a plan lives or dies on execution, the numbers speak for themselves).

AI substitutes elaborate verbs for simple "is," "are," "has," or gives agency to an abstraction instead of naming who or what actually did something. Use the simple verb, or name the real actor.

> Before: "Gallery 825 serves as LAAA's exhibition space. The gallery features four rooms and boasts over 3,000 square feet."
> After: "Gallery 825 is LAAA's exhibition space. The gallery has four rooms totaling 3,000 square feet."

> Before: "The data tells us that engagement declined this quarter."
> After: "Engagement declined this quarter."

**9. Negative parallelisms**
Watch for: Not only...but..., It's not just about..., it's..., It's not merely X, it's Y

These constructions are individually fine but AI overuses them for rhetorical emphasis where simple direct statements work better.

> Before: "It's not just about the beat riding under the vocals; it's part of the aggression and atmosphere. It's not merely a song, it's a statement."
> After: "The heavy beat adds to the aggressive tone."

**10. Rule of three**
AI forces ideas into groups of three to sound comprehensive. Often two of the three items are near-synonyms.

> Before: "The event features keynote sessions, panel discussions, and networking opportunities. Attendees can expect innovation, inspiration, and industry insights."
> After: "The event includes talks and panels, with time for informal networking between sessions."

**11. Synonym cycling (elegant variation)**
AI has repetition penalties that cause excessive synonym substitution for the same referent. Pick a term and stick with it.

> Before: "The protagonist faces many challenges. The main character must overcome obstacles. The central figure eventually triumphs. The hero returns home."
> After: "The protagonist faces many challenges but eventually triumphs and returns home."


### Style patterns (12-18)

**12. Em dash overuse**
AI uses em dashes more than humans typically do. One or two in a long piece is fine. Three in a paragraph is a tell. Substitute commas, periods, or parentheses.

> Before: "The term is primarily promoted by Dutch institutions—not by the people themselves. You don't say 'Netherlands, Europe'—yet this mislabeling continues—even in official documents."
> After: "The term is primarily promoted by Dutch institutions, not by the people themselves. You don't say 'Netherlands, Europe,' yet this mislabeling continues in official documents."

**13. Mechanical boldface**
AI emphasizes key terms in bold systematically. In most professional writing, bold should be rare and purposeful — used for warnings or truly critical callouts, not sprinkled through running text.

> Before: "It blends **OKRs**, **KPIs**, and visual strategy tools such as the **Business Model Canvas** and **Balanced Scorecard**."
> After: "It blends OKRs, KPIs, and visual strategy tools like the Business Model Canvas and Balanced Scorecard."

**14. Bold-header-colon list items**
AI loves lists where each item starts with a bolded label, a colon, and then a sentence. This format is occasionally useful but is wildly overused. Often the content works better as a paragraph.

> Before:
> - **User Experience:** Significantly improved with a new interface.
> - **Performance:** Enhanced through optimized algorithms.
> - **Security:** Strengthened with end-to-end encryption.

> After: "The update improves the interface, speeds up load times with optimized algorithms, and adds end-to-end encryption."

**15. Title Case headings**
AI capitalizes all main words in headings. Most modern style guides (AP, Google, many company style guides) use sentence case.

> Before: "Strategic Negotiations And Global Partnerships"
> After: "Strategic negotiations and global partnerships"

**16. Emoji decoration**
AI decorates headings and bullet points with emoji. This is almost never appropriate in professional writing. (Slack messages are a partial exception — an occasional emoji is fine if it matches team culture.)

**17. Curly quotation marks**
ChatGPT specifically uses curly/smart quotes instead of straight quotes. This is a minor tell but recognizable. Use straight quotes in technical contexts; either is fine in other writing, just be consistent.

**18. False ranges**
AI uses "from X to Y" constructions where X and Y aren't on a meaningful scale.

> Before: "Our journey has taken us from the singularity of the Big Bang to the grand cosmic web, from the birth of stars to the enigmatic dance of dark matter."
> After: "The book covers the Big Bang, star formation, and current theories about dark matter."


### Communication patterns (19-21)

**19. Chatbot residue**
Watch for: I hope this helps, Of course!, Certainly!, You're absolutely right!, Would you like..., Let me know if..., Here is a...

Conversation artifacts from chatbot interactions that get left in written output. Strip completely — these have no place in documents, emails, or messages.

**20. Knowledge-cutoff disclaimers**
Watch for: as of [date], Up to my last training update, While specific details are limited..., based on available information...

AI hedges about incomplete information. In professional writing, either find the information or omit the claim. Don't narrate your uncertainty about your own training data.

**21. Sycophantic tone**
Watch for: Great question!, That's an excellent point!, You raise a really important..., You're absolutely right!

People-pleasing language. In written documents this is always wrong. In emails, mild acknowledgment is fine ("Good point" or "Makes sense") but the AI version is way over the top.


### Filler and hedging (22-25)

**22. Filler phrases**
Direct replacements:
- "In order to achieve this goal" -> "To achieve this"
- "Due to the fact that" -> "Because"
- "At this point in time" -> "Now"
- "In the event that" -> "If"
- "Has the ability to" -> "Can"
- "It is important to note that" -> (cut entirely, just state the thing)
- "It is worth mentioning that" -> (cut entirely)
- "It should be noted that" -> (cut entirely)
- "When it comes to" -> (cut or rephrase)

**23. Excessive hedging**
AI over-qualifies everything. One hedge is fine. Three nested hedges kill the sentence.

> Before: "It could potentially possibly be argued that the policy might have some effect on outcomes."
> After: "The policy may affect outcomes."

**24. Generic positive conclusions**
Watch for: The future looks bright, Exciting times lie ahead, This represents a major step in the right direction, [Topic] continues to evolve

AI defaults to vague optimism at the end of any piece. End with something concrete or just stop writing.

> Before: "The future looks bright for the company. Exciting times lie ahead as they continue their journey toward excellence."
> After: "The company plans to open two more locations next year."

**25. Hyphenated compound modifiers**
AI hyphenates common compound modifiers with robotic consistency. Most professional style guides do recommend hyphenating compound modifiers before nouns (e.g., "high-quality output"), so don't strip hyphens that are grammatically correct. The tell is when every single compound modifier is hyphenated with perfect uniformity — real writers are inconsistent about this. Use your judgment: keep hyphens where they aid clarity, relax them where the meaning is obvious without them.

This pattern is a weak signal on its own. Don't change correct hyphenation just to seem "more human." Focus on the higher-signal patterns first.


---


## Output format

**Editing mode:**
1. The rewritten text
2. "Changes:" followed by 2-5 brief bullets noting the main things you fixed

**Generation mode:**
1. The text
2. Optionally, 1-3 sentences on tone/approach choices (only if it adds useful context — skip if the choices are self-evident)

**Detect-only mode:**
1. A short list: pattern name, the quoted offending text, and a one-line fix for each real match
2. No rewritten draft, unless the user asks for one after seeing the report
